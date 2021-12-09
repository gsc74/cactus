#!/usr/bin/env python3

#Released under the MIT license, see LICENSE.txt

"""Run the multiple alignment on pairwise alignment input (ie cactus_setup_phase and beyond)

"""
import os
from argparse import ArgumentParser
import xml.etree.ElementTree as ET
import copy
import timeit
import time
import multiprocessing
from operator import itemgetter

from cactus.shared.common import setupBinaries, importSingularityImage
from cactus.pipeline.cactus_workflow import cactus_cons_with_resources
from cactus.progressive.progressive_decomposition import compute_outgroups, compute_schedule, parse_seqfile, get_subtree, get_spanning_subtree, get_event_set
from cactus.progressive.cactus_progressive import export_hal
from cactus.shared.common import makeURL, catFiles
from cactus.shared.common import enableDumpStack
from cactus.shared.common import cactus_override_toil_options
from cactus.shared.common import findRequiredNode
from cactus.shared.common import getOptionalAttrib
from cactus.shared.common import cactus_call
from cactus.shared.common import write_s3, has_s3, get_aws_region, unzip_gzs
from cactus.shared.common import cactusRootPath
from cactus.shared.configWrapper import ConfigWrapper

from toil.job import Job
from toil.common import Toil
from toil.statsAndLogging import logger
from toil.statsAndLogging import set_logging_from_options
from toil.lib.threading import cpu_count

from sonLib.nxnewick import NXNewick
from sonLib.bioio import getTempDirectory, getTempFile

def main():
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)

    parser.add_argument("seqFile", help = "Seq file")
    parser.add_argument("pafFile", type=str, help = "Pairiwse aliginments (from cactus-blast, cactus-refmap or cactus-graphmap)")
    parser.add_argument("outHal", type=str, help = "Output HAL file (or directory in --batch mode)")
    parser.add_argument("--pathOverrides", nargs="*", help="paths (multiple allowd) to override from seqFile")
    parser.add_argument("--pathOverrideNames", nargs="*", help="names (must be same number as --paths) of path overrides")

    #Pangenome Options
    parser.add_argument("--pangenome", action="store_true",
                        help="Activate pangenome mode (suitable for star trees of closely related samples) by overriding several configuration settings."
                        " The overridden configuration will be saved in <outHal>.pg-conf.xml")
    parser.add_argument("--singleCopySpecies", type=str,
                        help="Filter out all self-alignments in given species")
    parser.add_argument("--barMaskFilter", type=int, default=None,
                        help="BAR's POA aligner will ignore softmasked regions greater than this length. (overrides partialOrderAlignmentMaskFilter in config)")
    parser.add_argument("--outVG", action="store_true", help = "export pangenome graph in VG (.vg) in addition to HAL")
    parser.add_argument("--outGFA", action="store_true", help = "export pangenome grpah in GFA (.gfa.gz) in addition to HAL")
    parser.add_argument("--batch", action="store_true", help = "Launch batch of alignments.  Input seqfile is expected to be chromfile as generated by cactus-graphmap-slit")
    parser.add_argument("--reference", type=str, help = "Ensure that given genome is acyclic by deleting all paralogy edges in postprocessing, also do not mask its PAF mappings")
    
    #Progressive Cactus Options
    parser.add_argument("--configFile", dest="configFile",
                        help="Specify cactus configuration file",
                        default=os.path.join(cactusRootPath(), "cactus_progressive_config.xml"))
    parser.add_argument("--root", dest="root", help="Name of ancestral node (which"
                        " must appear in NEWICK tree in <seqfile>) to use as a "
                        "root for the alignment.  Any genomes not below this node "
                        "in the tree may be used as outgroups but will never appear"
                        " in the output.  If no root is specifed then the root"
                        " of the tree is used. ", default=None)
    parser.add_argument("--latest", dest="latest", action="store_true",
                        help="Use the latest version of the docker container "
                        "rather than pulling one matching this version of cactus")
    parser.add_argument("--containerImage", dest="containerImage", default=None,
                        help="Use the the specified pre-built containter image "
                        "rather than pulling one from quay.io")
    parser.add_argument("--binariesMode", choices=["docker", "local", "singularity"],
                        help="The way to run the Cactus binaries", default=None)
    parser.add_argument("--gpu", action="store_true",
                        help="Enable GPU acceleration by using Segaling instead of lastz")
    parser.add_argument("--consCores", type=int, 
                        help="Number of cores for each cactus_consolidated job (defaults to all available / maxCores on single_machine)", default=None)

    options = parser.parse_args()

    setupBinaries(options)
    set_logging_from_options(options)
    enableDumpStack()

    if (options.pathOverrides or options.pathOverrideNames):
        if not options.pathOverrides or not options.pathOverrideNames or \
           len(options.pathOverrideNames) != len(options.pathOverrides):
            raise RuntimeError('same number of values must be passed to --pathOverrides and --pathOverrideNames')

    # Try to juggle --maxCores and --consCores to give some reasonable defaults where possible
    if options.batchSystem.lower() in ['single_machine', 'singlemachine']:
        if options.maxCores is not None:
            if int(options.maxCores) <= 0:
                raise RuntimeError('Cactus requires --maxCores >= 1')
        if options.consCores is None:
            if options.maxCores is not None:
                options.consCores = int(options.maxCores)
            else:
                options.consCores = cpu_count()
        elif options.maxCores is not None and options.consCores > int(options.maxCores):
            raise RuntimeError('--consCores must be <= --maxCores')
    else:
        if not options.consCores:
            raise RuntimeError('--consCores required for non single_machine batch systems')
    if options.maxCores is not None and options.consCores > int(options.maxCores):
        raise RuntimeError('--consCores must be <= --maxCores')

    options.buildHal = True
    options.buildFasta = True

    if options.outHal.startswith('s3://'):
        if not has_s3:
            raise RuntimeError("S3 support requires toil to be installed with [aws]")
        # write a little something to the bucket now to catch any glaring problems asap
        test_file = os.path.join(getTempDirectory(), 'check')
        with open(test_file, 'w') as test_o:
                test_o.write("\n")
        region = get_aws_region(options.jobStore) if options.jobStore.startswith('aws:') else None
        write_s3(test_file, options.outHal if options.outHal.endswith('.hal') else os.path.join(options.outHal, 'test'), region=region)
        options.checkpointInfo = (get_aws_region(options.jobStore), options.outHal)
    else:
        options.checkpointInfo = None
        
    if options.batch:
        # the output hal is a directory, make sure it's there
        if not os.path.isdir(options.outHal):
            os.makedirs(options.outHal)
        assert len(options.pafFile) == 0
    else:
        assert len(options.pafFile) > 0

    # Mess with some toil options to create useful defaults.
    cactus_override_toil_options(options)

    start_time = timeit.default_timer()
    with Toil(options) as toil:
        importSingularityImage(options)
        if options.restart:
            results_dict = toil.restart()
        else:
            align_jobs = make_batch_align_jobs(options, toil)
            results_dict = toil.start(Job.wrapJobFn(batch_align_jobs, align_jobs))

        # when using s3 output urls, things get checkpointed as they're made so no reason to export
        # todo: make a more unified interface throughout cactus for this
        # (see toil-vg's outstore logic which, while not perfect, would be an improvement
        if not options.outHal.startswith('s3://'):
            if options.batch:
                for chrom, results in results_dict.items():
                    toil.exportFile(results[0], makeURL(os.path.join(options.outHal, '{}.hal'.format(chrom))))
                    if options.outVG:
                        toil.exportFile(results[1], makeURL(os.path.join(options.outHal, '{}.vg'.format(chrom))))
                    if options.outGFA:
                        toil.exportFile(results[2], makeURL(os.path.join(options.outHal, '{}.gfa.gz'.format(chrom))))                    
            else:
                assert len(results_dict) == 1 and None in results_dict
                halID, vgID, gfaID = results_dict[None][0], results_dict[None][1], results_dict[None][2]
                # export the hal
                toil.exportFile(halID, makeURL(options.outHal))
                # export the vg
                if options.outVG:
                    toil.exportFile(vgID, makeURL(os.path.splitext(options.outHal)[0] + '.vg'))
                if options.outGFA:
                    toil.exportFile(gfaID, makeURL(os.path.splitext(options.outHal)[0] + '.gfa.gz'))
                                
    end_time = timeit.default_timer()
    run_time = end_time - start_time
    logger.info("cactus-align has finished after {} seconds".format(run_time))
    
def batch_align_jobs(job, jobs_dict):
    """ todo: clean this up """
    rv_dict = {}
    for chrom, chrom_job in jobs_dict.items():
        rv_dict[chrom] = job.addChild(chrom_job).rv()
    return rv_dict

def make_batch_align_jobs(options, toil):
    """ Make a dicitonary of align jobs """

    result_dict = {}    
    if options.batch is True:
        #read the chrom file
        with open(options.seqFile, 'r') as chrom_file:
            for line in chrom_file:
                toks = line.strip().split()
                if len(toks):
                    assert len(toks) == 3
                    chrom, seqfile, alnFile = toks[0], toks[1], toks[2]
                    chrom_options = copy.deepcopy(options)
                    chrom_options.batch = False
                    chrom_options.seqFile = seqfile
                    chrom_options.pafFile = [alnFile]
                    if chrom_options.checkpointInfo:
                        chrom_options.checkpointInfo = (chrom_options.checkpointInfo[0],
                                                        os.path.join(chrom_options.checkpointInfo[1], chrom + '.hal'))
                    chrom_align_job = make_align_job(chrom_options, toil)
                    result_dict[chrom] = chrom_align_job
    else:
        result_dict[None] = make_align_job(options, toil)

    return result_dict
    
    
def make_align_job(options, toil):

    # load up the seqfile and figure out the outgroups and schedule
    config_node = ET.parse(options.configFile).getroot()
    config_wrapper = ConfigWrapper(config_node)
    config_wrapper.substituteAllPredefinedConstantsWithLiterals()
    # apply gpu override
    config_wrapper.initGPU(options.gpu)
    mc_tree, input_seq_map, og_candidates = parse_seqfile(options.seqFile, config_wrapper)
    og_map = compute_outgroups(mc_tree, config_wrapper, set(og_candidates), options.root)
    event_set = get_event_set(mc_tree, config_wrapper, og_map, options.root)
    
    # apply path overrides.  this was necessary for wdl which doesn't take kindly to
    # text files of local paths (ie seqfile).  one way to fix would be to add support
    # for s3 paths and force wdl to use it.  a better way would be a more fundamental
    # interface shift away from files of paths throughout all of cactus
    if options.pathOverrides:
        for name, override in zip(options.pathOverrideNames, options.pathOverrides):
            input_seq_map[name] = override

    # infer default root
    if not options.root:
        options.root = mc_tree.getRootName()

    # check --reference input
    if options.reference:
        leaves = [tree.getName(leaf) for leaf in mc_tree.getLeaves()]
        if options.reference not in leaves:
            raise RuntimeError("Genome specified with --reference, {}, not found in tree leaves".format(options.reference))

    # toggle stable
    paf_to_stable = False
    config_node = ET.parse(options.configFile).getroot()
    if getOptionalAttrib(findRequiredNode(config_node, "graphmap"), "removeMinigraphFromPAF", typeFn=bool, default=False):
        # remove minigraph event from input
        graph_event = getOptionalAttrib(findRequiredNode(config_node, "graphmap"), "assemblyName", default="_MINIGRAPH_")
        if graph_event in event_set:
            event_set.remove(graph_event)
            mc_tree.removeLeaf(tree.getNodeId(graph_event))
            paf_to_stable = True

    # apply command line overrides to config
    cafNode = findRequiredNode(config_node, "caf")
    barNode = findRequiredNode(config_node, "bar")
    poaNode = findRequiredNode(barNode, "poa")
    if options.singleCopySpecies:
        cafNode.attrib["alignmentFilter"] = "singleCopyEvent:{}".format(options.singleCopySpecies)
    if options.barMaskFilter:
        poaNode.attrib["partialOrderAlignmentMaskFilter"] = str(options.barMaskFilter)
    if options.pangenome:
        # turn off the megablock filter as it ruins non-all-to-all alignments
        cafNode.attrib["minimumBlockHomologySupport"] = "0"
        cafNode.attrib["minimumBlockDegreeToCheckSupport"] = "9999999999"
        # turn off mapq filtering
        cafNode.attrib["runMapQFiltering"] = "0"
        # more iterations here helps quite a bit to reduce underalignment
        cafNode.attrib["maxRecoverableChainsIterations"] = "50"
        # turn down minimum block degree to get a fat ancestor
        barNode.attrib["minimumBlockDegree"] = "1"
        # turn on POA
        barNode.attrib["partialOrderAlignment"] = "1"
        # turn off POA seeding
        poaNode.attrib["partialOrderAlignmentDisableSeeding"] = "1"

    # import the PAF alignments
    paf_id = toil.importFile(makeURL(options.pafFile))
    
    #import the sequences
    input_seq_id_map = {}
    for (genome, seq) in input_seq_map.items():
        if genome in event_set:
            if os.path.isdir(seq):
                tmpSeq = getTempFile()
                catFiles([os.path.join(seq, subSeq) for subSeq in os.listdir(seq)], tmpSeq)
                seq = tmpSeq
            seq = makeURL(seq)
            logger.info("Importing {}".format(seq))
            input_seq_id_map[genome] = toil.importFile(seq)

    # make the align job that will (optional unzip) -> consolidated -> hal export -> (optional vg/gfa)
    align_job = Job.wrapJobFn(cactus_align,
                              config_wrapper,
                              mc_tree,
                              input_seq_map,
                              input_seq_id_map,
                              paf_id,
                              options.root,
                              og_map,
                              checkpointInfo=options.checkpointInfo,
                              doVG=options.outVG,
                              doGFA=options.outGFA,
                              referenceEvent=options.reference,
                              paf2Stable=paf_to_stable,
                              cons_cores = options.consCores)
    return align_job

def cactus_align(job, config_wrapper, mc_tree, input_seq_map, input_seq_id_map, paf_id, root_name, og_map, checkpointInfo, doVG, doGFA,
                 referenceEvent=None, paf2Stable=False, cons_cores = None):
    
    head_job = Job()
    job.addChild(head_job)

    event_list = input_seq_id_map.keys()
    
    # unzip the input sequences
    unzip_job = head_job.addChildJobFn(unzip_gzs, [input_seq_map[e] for e in event_list], [input_seq_id_map[e] for e in event_list])

    new_seq_id_map = {}
    for i, event in enumerate(input_seq_id_map.keys()):
        new_seq_id_map[event] = unzip_job.rv(i)

    # get the spanning tree (which is what consolidated wants)
    spanning_tree = get_spanning_subtree(mc_tree, root_name, config_wrapper, og_map)

    # run consolidated
    cons_job = head_job.addFollowOnJobFn(cactus_cons_with_resources, spanning_tree, root_name, config_wrapper.xmlRoot, new_seq_id_map, og_map, paf_id, cons_cores = cons_cores)

    # get the immediate subtree (which is all export_hal can use)
    sub_tree = get_subtree(mc_tree, root_name, config_wrapper, og_map, include_outgroups=False)
    
    # run the hal export
    hal_job = cons_job.addFollowOnJobFn(export_hal, sub_tree, config_wrapper.xmlRoot, new_seq_id_map, og_map, [cons_job.rv()], event=root_name, inMemory=True,
                                        checkpointInfo=checkpointInfo, acyclicEvent=referenceEvent)

    # optionally create the VG
    if doVG or doGFA:
        vg_export_job = hal_export_job.addFollowOnJobFn(export_vg, hal_job.rv(), config_wrapper, doVG, doGFA,
                                                        checkpointInfo=checkpointInfo)
        vg_file_id, gfa_file_id = vg_export_job.rv(0), vg_export_job.rv(1)
    else:
        vg_file_id, gfa_file_id = None, None
        
    return hal_job.rv(), vg_file_id, gfa_file_id


def export_vg(job, hal_id, config_wrapper, doVG, doGFA, checkpointInfo=None, resource_spec = False):
    """ use hal2vg to convert the HAL to vg format """

    if not resource_spec:
        # caller couldn't figure out the resrouces from hal_id promise.  do that
        # now and try again
        return job.addChildJobFn(export_vg, hal_id, config_wrapper, doVG, doGFA, checkpointInfo,
                                 resource_spec = True,
                                 disk=hal_id.size * 3,
                                 memory=hal_id.size * 10).rv()
        
    work_dir = job.fileStore.getLocalTempDir()
    hal_path = os.path.join(work_dir, "out.hal")
    job.fileStore.readGlobalFile(hal_id, hal_path)
    
    graph_event = getOptionalAttrib(findRequiredNode(config_wrapper.xmlRoot, "graphmap"), "assemblyName", default="_MINIGRAPH_")
    hal2vg_opts = getOptionalAttrib(findRequiredNode(config_wrapper.xmlRoot, "hal2vg"), "hal2vgOptions", default="")
    if hal2vg_opts:
        hal2vg_opts = hal2vg_opts.split(' ')
    else:
        hal2vg_opts = []
    ignore_events = []
    if not getOptionalAttrib(findRequiredNode(config_wrapper.xmlRoot, "hal2vg"), "includeMinigraph", typeFn=bool, default=False):
        ignore_events.append(graph_event)
    if not getOptionalAttrib(findRequiredNode(config_wrapper.xmlRoot, "hal2vg"), "includeAncestor", typeFn=bool, default=False):
        ignore_events.append(config_wrapper.getDefaultInternalNodePrefix() + '0')
    if ignore_events:
        hal2vg_opts += ['--ignoreGenomes', ','.join(ignore_events)]
    if not getOptionalAttrib(findRequiredNode(config_wrapper.xmlRoot, "hal2vg"), "prependGenomeNames", typeFn=bool, default=True):
        hal2vg_opts += ['--onlySequenceNames']

    vg_path = os.path.join(work_dir, "out.vg")
    cmd = ['hal2vg', hal_path] + hal2vg_opts

    cactus_call(parameters=cmd, outfile=vg_path)

    if checkpointInfo:
        write_s3(vg_path, os.path.splitext(checkpointInfo[1])[0] + '.vg', region=checkpointInfo[0])

    gfa_path = os.path.join(work_dir, "out.gfa.gz")
    if doGFA:
        gfa_cmd = [ ['vg', 'view', '-g', vg_path], ['gzip'] ]
        cactus_call(parameters=gfa_cmd, outfile=gfa_path)

        if checkpointInfo:
            write_s3(gfa_path, os.path.splitext(checkpointInfo[1])[0] + '.gfa.gz', region=checkpointInfo[0])

    vg_id = job.fileStore.writeGlobalFile(vg_path) if doVG else None
    gfa_id = job.fileStore.writeGlobalFile(gfa_path) if doGFA else None

    return vg_id, gfa_id

def main_batch():
    """ this is a bit like cactus-align --batch except it will use toil-in-toil to assign each chromosome to a machine.
    pros: much less chance of a problem with one chromosome affecting anything else
          more forgiving for inexact resource specs
          could be ported to Terra
    cons: less efficient use of resources
    """
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    addCactusWorkflowOptions(parser)

    parser.add_argument("chromFile", help = "chroms file")
    parser.add_argument("outHal", type=str, help = "Output directory (can be s3://)")
    parser.add_argument("--alignOptions", type=str, help = "Options to pass through to cactus-align (don't forget to wrap in quotes)")
    parser.add_argument("--alignCores", type=int, help = "Number of cores per align job")
    parser.add_argument("--alignCoresOverrides", nargs="*", help = "Override align job cores for a chromosome. Space-separated list of chrom,cores pairse epxected")

    parser.add_argument("--configFile", dest="configFile",
                        help="Specify cactus configuration file",
                        default=os.path.join(cactusRootPath(), "cactus_progressive_config.xml"))

    options = parser.parse_args()

    options.containerImage=None
    options.binariesMode=None
    options.root=None
    options.latest=None

    setupBinaries(options)
    set_logging_from_options(options)
    enableDumpStack()

    # Mess with some toil options to create useful defaults.
    cactus_override_toil_options(options)

    # Turn the overrides into a dict
    cores_overrides = {}
    if options.alignCoresOverrides:
        for o in options.alignCoresOverrides:
            try:
                chrom, cores = o.split(',')
                cores_overrides[chrom] = int(cores)
            except:
                raise RuntimeError("Error parsing alignCoresOverrides \"{}\"".format(o))
    options.alignCoresOverrides = cores_overrides                

    start_time = timeit.default_timer()
    with Toil(options) as toil:
        importSingularityImage(options)
        if options.restart:
            results_dict = toil.restart()
        else:
            config_id = toil.importFile(makeURL(options.configFile))
            # load the chromfile into memory
            chrom_dict = {}
            with open(options.chromFile, 'r') as chrom_file:
                for line in chrom_file:
                    toks = line.strip().split()
                    if len(toks):
                        assert len(toks) == 3
                        chrom, seqfile, alnFile = toks[0], toks[1], toks[2]
                        chrom_dict[chrom] = toil.importFile(makeURL(seqfile)), toil.importFile(makeURL(alnFile))
            results_dict = toil.start(Job.wrapJobFn(align_toil_batch, chrom_dict, config_id, options))

        # when using s3 output urls, things get checkpointed as they're made so no reason to export
        # todo: make a more unified interface throughout cactus for this
        # (see toil-vg's outstore logic which, while not perfect, would be an improvement
        if not options.outHal.startswith('s3://'):
            if options.batch:
                for chrom, results in results_dict.items():
                    toil.exportFile(results[0], makeURL(os.path.join(options.outHal, '{}.hal'.format(chrom))))
                    if options.outVG:
                        toil.exportFile(results[1], makeURL(os.path.join(options.outHal, '{}.vg'.format(chrom))))
                    if options.outGFA:
                        toil.exportFile(results[2], makeURL(os.path.join(options.outHal, '{}.gfa.gz'.format(chrom))))
                    toil.exportFile(results[3], makeURL(os.path.join(options.outHal, '{}.hal.log'.format(chrom))))
                                
    end_time = timeit.default_timer()
    run_time = end_time - start_time
    logger.info("cactus-align-batch has finished after {} seconds".format(run_time))

def align_toil_batch(job, chrom_dict, config_id, options):
    """ spawn a toil job for each cactus-align """

    results_dict = {}
    for chrom in chrom_dict.keys():
        seq_file_id, paf_file_id = chrom_dict[chrom]
        align_job = job.addChildJobFn(align_toil, chrom, seq_file_id, paf_file_id, config_id, options,
                                      cores=options.alignCoresOverrides[chrom] if chrom in options.alignCoresOverrides else options.alignCores)
        results_dict[chrom] = align_job.rv()

    return results_dict

def align_toil(job, chrom, seq_file_id, paf_file_id, config_id, options):
    """ run cactus-align """
    
    work_dir = job.fileStore.getLocalTempDir()
    config_file = os.path.join(work_dir, 'config.xml')
    job.fileStore.readGlobalFile(config_id, config_file)

    seq_file = os.path.join(work_dir, '{}_seq_file.txt'.format(chrom))
    job.fileStore.readGlobalFile(seq_file_id, seq_file)

    paf_file = os.path.join(work_dir, '{}.paf'.format(chrom))
    job.fileStore.readGlobalFile(paf_file_id, paf_file)

    js = os.path.join(work_dir, 'js')

    if options.outHal.startswith('s3://'):
        out_file = os.path.join(options.outHal, '{}.hal'.format(chrom))
    else:
        out_file = os.path.join(work_dir, '{}.hal'.format(chrom))

    log_file = os.path.join(work_dir, '{}.hal.log'.format(chrom))

    cmd = ['cactus-align', js, seq_file, paf_file, out_file, '--logFile', log_file, '--configFile', config_file] + options.alignOptions.split()

    cactus_call(parameters=cmd)

    ret_ids = [None, None, None, None]

    if not options.outHal.startswith('s3://'):
        # we're not checkpoint directly to s3, so we return 
        ret_ids[0] = job.fileStore.writeGlobalFile(out_file)
        out_vg = os.path.splitext(out_file)[0] + '.vg'
        if os.path.exists(out_vg):
            ret_ids[1] = job.fileStore.writeGlobalFile(out_vg)
        out_gfa = os.path.splitext(out_file)[0] + '.gfa.gz'
        if os.path.exists(out_gfa):
            ret_ids[2] = job.fileStore.writeGlobalFile(out_gfa)
        ret_ids[3] = job.fileStore.writeGlobalFile(log_file)
    else:
        write_s3(log_file, out_file + '.log', region=get_aws_region(options.jobStore))            

    return ret_ids

if __name__ == '__main__':
    main()
