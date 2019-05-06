import os
import queue
from time import sleep
from collections import namedtuple

import mappy
import pysam

from megalodon import megalodon_helper as mh


MAP_POS = namedtuple('MAP_POS', (
    'chrm', 'strand', 'start', 'end', 'q_trim_start', 'q_trim_end'))


# mappy aligner with extra attrs
class alignerPlus(mappy.Aligner):
    ref_names_and_lens = None
    out_fmt = None
    ref_fn = None

    def add_ref_names(self, ref_fn):
        # extract reference names and lengths
        with pysam.FastaFile(ref_fn) as ref:
            self.ref_names_and_lens = (
                ref.references, ref.lengths)

        return


def align_read(q_seq, aligner, map_thr_buf, read_id=None, add_chr_ref=False):
    try:
        r_algn = next(aligner.map(str(q_seq), buf=map_thr_buf))
    except StopIteration:
        # alignment not produced
        return [None, None], None

    ref_seq = aligner.seq(r_algn.ctg, r_algn.r_st, r_algn.r_en)
    if r_algn.strand == -1:
        ref_seq = mh.revcomp(ref_seq)
    chrm = 'chr' + r_algn.ctg if add_chr_ref else r_algn.ctg
    r_algn_data = [
        chrm, r_algn.strand, r_algn.r_st, r_algn.r_en,
        r_algn.q_st, r_algn.q_en, r_algn.cigar]
    return [ref_seq, r_algn_data], (
        read_id, q_seq, r_algn.ctg, r_algn.strand, r_algn.r_st,
        r_algn.q_st, r_algn.q_en, r_algn.cigar)


def _map_read_worker(aligner, map_conn, mo_q, add_chr_ref):
    # get mappy aligner thread buffer
    map_thr_buf = mappy.ThreadBuffer()

    while True:
        try:
            q_seq, read_id = map_conn.recv()
        except:
            # exit gracefully
            return
        if q_seq is None:
            break
        map_res, full_res = align_read(
            q_seq, aligner, map_thr_buf, read_id, add_chr_ref)
        map_conn.send(map_res)

        if mo_q is not None and full_res is not None:
            mo_q.put((map_res[0], full_res))

    return


def parse_cigar(r_cigar, strand):
    # get each base calls genomic position
    r_to_q_poss = {}
    # process cigar ops in read direction
    curr_r_pos, curr_q_pos = 0, 0
    cigar_ops = r_cigar if strand == 1 else r_cigar[::-1]
    for op_len, op in cigar_ops:
        if op == 1:
            # inserted bases into ref
            curr_q_pos += op_len
        elif op in (2, 3):
            # deleted ref bases
            for r_pos in range(curr_r_pos, curr_r_pos + op_len):
                r_to_q_poss[r_pos] = curr_q_pos
            curr_r_pos += op_len
        elif op in (0, 7, 8):
            # aligned bases
            for op_offset in range(op_len):
                r_to_q_poss[curr_r_pos + op_offset] = curr_q_pos + op_offset
            curr_q_pos += op_len
            curr_r_pos += op_len
        elif op == 6:
            # padding (shouldn't happen in mappy)
            pass
    r_to_q_poss[curr_r_pos] = curr_q_pos

    return r_to_q_poss


def map_read(q_seq, read_id, caller_conn):
    """Map read (query) sequence and return:
    1) reference sequence (endcoded as int labels)
    2) mapping from reference to read positions (after trimming)
    3) reference mapping position (including read trimming positions)
    """
    # send seq to _map_read_worker and receive mapped seq and pos
    caller_conn.send((q_seq, read_id))
    r_ref_seq, r_algn = caller_conn.recv()
    if r_ref_seq is None:
        raise mh.MegaError('No alignment')
    chrm, strand, r_st, r_en, q_st, q_en, r_cigar = r_algn

    r_to_q_poss = parse_cigar(r_cigar, strand)
    r_pos = MAP_POS(
        chrm=chrm, strand=strand, start=r_st, end=r_en,
        q_trim_start=q_st, q_trim_end=q_en)

    return r_ref_seq, r_to_q_poss, r_pos, r_cigar

def compute_pct_identity(cigar):
    nalign, nmatch = 0, 0
    for op_len, op in cigar:
        if op not in (4, 5): nalign += op_len
        if op in (0, 7): nmatch += op_len
    return 100 * nmatch / float(nalign)

def read_passes_filters(pr_ref_filts, read_len, q_st, q_en, cigar):
    if pr_ref_filts.min_len is not None and read_len < pr_ref_filts.min_len:
        return False
    if pr_ref_filts.max_len is not None and read_len > pr_ref_filts.max_len:
        return False
    if (pr_ref_filts.pct_cov is not None and
        100 * (q_en - q_st) / read_len < pr_ref_filts.pct_cov):
        return False
    if (pr_ref_filts.pct_idnt is not None and
        compute_pct_identity(cigar) < pr_ref_filts.pct_idnt):
        return False
    return True

def _get_map_queue(
        mo_q, map_conn, out_dir, ref_names_and_lens, map_fmt, ref_fn,
        do_output_pr_refs, pr_ref_filts):
    def write_alignment(
            read_id, q_seq, chrm, strand, r_st, q_st, q_en, cigar):
        q_seq = q_seq[q_st:q_en]
        if strand == -1:
            cigar = cigar[::-1]

        a = pysam.AlignedSegment()
        a.query_name = read_id
        a.query_sequence = q_seq if strand == 1 else mh.revcomp(q_seq)
        a.flag = 0 if strand == 1 else 16
        a.reference_id = map_fp.get_tid(chrm)
        a.reference_start = r_st
        a.cigartuples = [(op, op_l) for op_l, op in cigar]
        a.template_length = q_en - q_st
        map_fp.write(a)

        nalign, nmatch, ndel, nins = [0,] * 4
        for op_len, op in cigar:
            if op not in (4, 5): nalign += op_len
            if op in (0, 7): nmatch += op_len
            elif op in (2, 3): ndel += op_len
            elif op == 1: nins += op_len
        # compute alignment stats
        summ_fp.write('{}\t{:.2f}\t{}\t{}\t{}\t{}\n'.format(
            read_id, 100 * nmatch / float(nalign), nalign, nmatch, ndel, nins))
        summ_fp.flush()

        return

    def write_pr_ref(read_id, ref_seq):
        pr_ref_fp.write('>{}\n{}\n'.format(read_id, ref_seq))
        pr_ref_fp.flush()
        return

    def get_alignment():
        ref_seq, (read_id, q_seq, chrm, strand, r_st, q_st, q_en,
                  cigar) = mo_q.get(block=False)
        write_alignment(read_id, q_seq, chrm, strand, r_st, q_st, q_en, cigar)
        if do_output_pr_refs:
            if read_passes_filters(
                    pr_ref_filts, len(q_seq), q_st, q_en, cigar):
                write_pr_ref(read_id, ref_seq)
        return


    map_bn, summ_fn = mh.OUTPUT_FNS[mh.MAP_NAME]

    summ_fp = open(os.path.join(out_dir, summ_fn), 'w')
    summ_fp.write('read_id\tpct_identity\tnum_align\tnum_match\t' +
                  'num_del\tnum_ins\n')

    map_fn = os.path.join(out_dir, map_bn + '.' + map_fmt)
    if map_fmt == 'bam': w_mode = 'wb'
    elif map_fmt == 'cram': w_mode = 'wc'
    elif map_fmt == 'sam': w_mode = 'w'
    else:
        raise mh.MegaError('Invalid mapping output format')
    map_fp = pysam.AlignmentFile(
        map_fn, w_mode, reference_names=ref_names_and_lens[0],
        reference_lengths=ref_names_and_lens[1], reference_filename=ref_fn)

    if do_output_pr_refs:
        pr_ref_fp = open(os.path.join(out_dir, mh.PR_REF_FN), 'w')

    try:
        while True:
            try:
                get_alignment()
            except queue.Empty:
                if map_conn.poll():
                    break
                sleep(0.1)
                continue

        while not mo_q.empty():
            get_alignment()
    finally:
        map_fp.close()
        summ_fp.close()
        if do_output_pr_refs:
            pr_ref_fp.close()

    return


if __name__ == '__main__':
    sys.stderr.write('This is a module. See commands with `megalodon -h`')
    sys.exit(1)
