from __future__ import with_statement
import json
import time
import random
import string
import logging
import os

from pprint import pformat
from os.path import abspath, dirname
from os.path import join as pjoin
from fabric.api import *
from fabric.contrib.console import confirm
from fabric.contrib.project import rsync_project
from awstools.afitools import *
from awstools.awstools import send_firesim_notification
from util.streamlogger import StreamLogger, InfoStreamLogger

rootLogger = logging.getLogger()

def get_deploy_dir():
    """ Must use local here. determine where the firesim/deploy dir is """
    with StreamLogger('stdout'), StreamLogger('stderr'):
        deploydir = local("pwd", capture=True)
    return deploydir

def sim_local(cmdline, capture=False):
    """ setup environment and locally execute a cmdline in sim subdir"""
    ddir = get_deploy_dir()
    with prefix('cd ' + ddir + '/../'), \
         prefix('export RISCV={}'.format(os.getenv('RISCV', ""))), \
         prefix('export PATH={}'.format(os.getenv('PATH', ""))), \
         prefix('export LD_LIBRARY_PATH={}'.format(os.getenv('LD_LIBRARY_PATH', ""))), \
         prefix('source ./sourceme-f1-manager.sh'), \
         prefix('cd sim/'), \
         InfoStreamLogger('stdout'), \
         InfoStreamLogger('stderr'):
        return local(cmdline, capture=capture)

def get_sim_makevar(buildconfig, varname):
    """ Dump make database for buildconfig returning value for varname assignment """
    cmdline = "{} | grep -E '^{} :?= '".format(
        buildconfig.make_recipe('echo ECHOVAR="{}"'.format(varname)),
        varname
    )

    res = sim_local(cmdline, capture=True)
    var_results = res.stdout.splitlines()
    assert len(var_results) == 1, "get_sim_makevar() command " + \
           res.real_command + " STDOUT is not a single line. It was:\n" + pformat(var_results)
    return var_results[0].split(None, 2)[-1]

def replace_rtl_local(conf, buildconfig):
    """ Run chisel/firrtl/fame-1, produce verilog for fpga build.

    THIS ALWAYS RUNS LOCALLY"""
    builddir = buildconfig.get_build_dir_name()
    fpgabuilddir = "hdk/cl/developer_designs/cl_" + buildconfig.get_chisel_triplet()
    ddir = get_deploy_dir()

    rootLogger.info("Running replace-rtl to generate verilog for " + str(buildconfig.get_chisel_triplet()))

    sim_local(buildconfig.make_recipe('replace-rtl'))
    with prefix('export CL_DIR={}/../platforms/f1/aws-fpga/{}'.format(ddir, fpgabuilddir)), \
         InfoStreamLogger('stdout'), \
         InfoStreamLogger('stderr'):
        run("""mkdir -p {}/results-build/{}/""".format(ddir, builddir))
        run("""cp $CL_DIR/design/cl_firesim_generated.sv {}/results-build/{}/cl_firesim_generated.sv""".format(ddir, builddir))

    build_driver(conf, buildconfig)

def gen_replace_rtl_script(conf, buildconfig):
    """ Run SBT assembly to create FAT jar to send to build hosts along with config-specific shell-script
        to execute the FAT jar

    THIS ALWAYS RUNS LOCALLY"""

    rootLogger.info("Generating FAT jar and replace-rtl.sh for " + str(buildconfig.get_chisel_triplet()))
    sim_local(buildconfig.make_recipe("--quiet gen-replace-rtl-script"))

def build_driver(conf, buildconfig):
    """ Build the FireSim-f1 driver for a given config

    THIS ALWAYS RUNS LOCALLY"""
    rootLogger.info("Building FireSim-f1 driver for " + str(buildconfig.get_chisel_triplet()))
    sim_local(buildconfig.make_recipe("f1"))

@parallel
def aws_build(global_build_config, bypass=False):
    """ Generate Verilog, Run Vivado, convert tar -> AGFI/AFI. Then terminate the instance at the end.
    conf = buildconfig dicitonary
    bypass: since this function takes a long time, bypass just returns for
    testing purposes when set to True. """
    if bypass:
        ### This is duplicated from the end of the function.
        buildconfig = global_build_config.get_build_by_ip(env.host_string)
        buildconfig.terminate_build_instance(buildconfig)
        return

    # The default error-handling procedure. Send an email and teardown instance
    def on_build_failure():
        message_title = "FireSim FPGA Build Failed"

        message_body = "Your FPGA build failed for triplet: " + buildconfig.get_chisel_triplet()
        message_body += ".\nInspect the log output from IP address " + env.host_string + " for more information."

        send_firesim_notification(message_title, message_body)

        rootLogger.info(message_title)
        rootLogger.info(message_body)
        rootLogger.info("Terminating the build instance now.")
        buildconfig.terminate_build_instance()


    rootLogger.info("Running process to build AGFI from verilog.")

    # First, Produce dcp/tar for design. Runs on remote machines at same path as manager
    ddir = get_deploy_dir()
    dparent = abspath(ddir + "/..")
    aws_fpga_root = pjoin(dparent, "platforms/f1/aws-fpga")
    buildconfig = global_build_config.get_build_by_ip(env.host_string)
    results_builddir = buildconfig.get_build_dir_name()
    # AWS build directory
    fpgabuilddir = "hdk/cl/developer_designs/cl_" + buildconfig.get_chisel_triplet()
    fpgatemplatedir = 'hdk/cl/developer_designs/cl_firesim'
    generated_dir = get_sim_makevar(buildconfig, 'GENERATED_DIR')
    fat_jar = get_sim_makevar(buildconfig, 'FAT_JAR')

    # first, copy aws-fpga to the build instance. it will live in
    # the same path on the build host as on the manager
    with StreamLogger('stdout'), StreamLogger('stderr'):
        run('mkdir -p ' + ddir)
        run('mkdir -p ' + generated_dir)
        run('mkdir -p ' + dirname(fat_jar))
        run('mkdir -p ' + aws_fpga_root)
        run('mkdir -p ' + pjoin(aws_fpga_root, fpgatemplatedir))
        run('mkdir -p ' + pjoin(aws_fpga_root, fpgabuilddir))

    # do the rsync, but ignore any checkpoints that might exist on this machine
    # (in case builds were run locally)
    # extra_opts -l preserves symlinks
    with StreamLogger('stdout'), StreamLogger('stderr'):
        rsync_cap = rsync_project(local_dir=pjoin(aws_fpga_root,''),
                      remote_dir=aws_fpga_root,
                      ssh_opts="-o StrictHostKeyChecking=no",
                      exclude="hdk/cl/developer_designs/cl_*",
                      extra_opts="-l", capture=True)
        rootLogger.debug(rsync_cap)
        rootLogger.debug(rsync_cap.stderr)

    if global_build_config.distributed_elaboration:
        with StreamLogger('stdout'), StreamLogger('stderr'):
            rsync_cap = rsync_project(local_dir=pjoin(aws_fpga_root, fpgatemplatedir, ''),
                  remote_dir=pjoin(aws_fpga_root, fpgatemplatedir),
                  exclude='build/checkpoints',
                  ssh_opts="-o StrictHostKeyChecking=no",
                  extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
            # sync the generated_dir (in a clean build, it is only the replace-rtl.sh)
            rsync_cap = rsync_project(local_dir=pjoin(generated_dir, ''),
                          remote_dir=generated_dir,
                          ssh_opts="-o StrictHostKeyChecking=no",
                          extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
            # sync the fat jar
            rsync_cap = rsync_project(local_dir=fat_jar,
                          remote_dir=fat_jar,
                          ssh_opts="-o StrictHostKeyChecking=no",
                          extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
        with InfoStreamLogger('stdout'), InfoStreamLogger('stderr'):
            # install java and dtc on the build host
            sudo('yum install -y java dtc')
            # run the replace-rtl.sh script
            rootLogger.info("Running process to build verilog from FAT jar.")
            run('bash -xe ' + pjoin(generated_dir,'replace-rtl.sh'))
        with StreamLogger('stdout'), StreamLogger('stderr'):
            # rsync generated_dir back to manager
            rsync_cap = rsync_project(local_dir=generated_dir,
                          remote_dir=pjoin(generated_dir, ''),
                          ssh_opts="-o StrictHostKeyChecking=no", upload=False, extra_opts="-l",
                          capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
            # rsync fpgabuilddir to manager
            rsync_cap = rsync_project(local_dir=pjoin(aws_fpga_root, fpgabuilddir),
                                      remote_dir=pjoin(aws_fpga_root, fpgabuilddir, ''),
                                      ssh_opts="-o StrictHostKeyChecking=no", upload=False, extra_opts="-l",
                                      capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
            local("mkdir -p {}/results-build/{}/".format(ddir, results_builddir))
            local("cp {}/design/cl_firesim_generated.sv {}/results-build/{}/cl_firesim_generated.sv".format(
                  pjoin(aws_fpga_root, fpgabuilddir),
                  ddir,
                  results_builddir))
    else:
        with StreamLogger('stdout'), StreamLogger('stderr'):
            # Verilog was built locally and fpgabuilddir is already populated, copy that over to build host
            rsync_cap = rsync_project(local_dir=pjoin(aws_fpga_root, fpgabuilddir, ''),
                  remote_dir=pjoin(aws_fpga_root, fpgabuilddir),
                  exclude='build/checkpoints',
                  ssh_opts="-o StrictHostKeyChecking=no",
                  extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)

    # run the Vivado build
    vivado_result = 0
    rootLogger.info("Running process to build AGFI from verilog.")
    with prefix('cd ' + aws_fpga_root), \
         prefix('source hdk_setup.sh'), \
         prefix('export CL_DIR=' + pjoin(aws_fpga_root, fpgabuilddir)), \
         prefix('cd $CL_DIR/build/scripts/'), InfoStreamLogger('stdout'), InfoStreamLogger('stderr'), \
         settings(warn_only=True):
        vivado_result = run('./aws_build_dcp_from_cl.sh -foreground').return_code

    # rsync in the reverse direction to get build results
    with StreamLogger('stdout'), StreamLogger('stderr'):
        rsync_cap = rsync_project(local_dir=pjoin(ddir, "results-build", results_builddir, "cl_firesim"),
                      remote_dir=pjoin(aws_fpga_root, fpgabuilddir, ''),
                      ssh_opts="-o StrictHostKeyChecking=no", upload=False, extra_opts="-l",
                      capture=True)
        rootLogger.debug(rsync_cap)
        rootLogger.debug(rsync_cap.stderr)

    if vivado_result != 0:
        on_build_failure()
        return

    if not aws_create_afi(global_build_config, buildconfig):
        on_build_failure()
        return


    rootLogger.info("Terminating the build instance now.")
    buildconfig.terminate_build_instance()


def aws_create_afi(global_build_config, buildconfig):
    """
    Convert the tarball created by Vivado build into an Amazon Global FPGA Image (AGFI)

    :return: None on error
    """
    ## next, do tar -> AGFI
    ## This is done on the local copy

    ddir = get_deploy_dir()
    results_builddir = buildconfig.get_build_dir_name()

    afi = None
    agfi = None
    s3bucket = global_build_config.s3_bucketname
    afiname = buildconfig.name

    # construct the "tags" we store in the AGFI description
    tag_buildtriplet = buildconfig.get_chisel_triplet()
    tag_deploytriplet = tag_buildtriplet
    if buildconfig.deploytriplet != "None":
        tag_deploytriplet = buildconfig.deploytriplet

    # the asserts are left over from when we tried to do this with tags
    # - technically I don't know how long these descriptions are allowed to be,
    # but it's at least 256*3, so I'll leave these here for now as sanity
    # checks.
    assert len(tag_buildtriplet) <= 255, "ERR: aws does not support tags longer than 256 chars for buildtriplet"
    assert len(tag_deploytriplet) <= 255, "ERR: aws does not support tags longer than 256 chars for deploytriplet"

    with StreamLogger('stdout'), StreamLogger('stderr'):
        is_dirty_str = local("if [[ $(git status --porcelain) ]]; then echo '-dirty'; fi", capture=True)
        hash = local("git rev-parse HEAD", capture=True)
    tag_fsimcommit = hash + is_dirty_str

    assert len(tag_fsimcommit) <= 255, "ERR: aws does not support tags longer than 256 chars for fsimcommit"

    # construct the serialized description from these tags.
    description = firesim_tags_to_description(tag_buildtriplet, tag_deploytriplet, tag_fsimcommit)

    # if we're unlucky, multiple vivado builds may launch at the same time. so we
    # append the build node IP + a random string to diff them in s3
    global_append = "-" + str(env.host_string) + "-" + ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(10)) + ".tar"

    with lcd("""{}/results-build/{}/cl_firesim/build/checkpoints/to_aws/""".format(ddir, results_builddir)), StreamLogger('stdout'), StreamLogger('stderr'):
        files = local('ls *.tar', capture=True)
        rootLogger.debug(files)
        rootLogger.debug(files.stderr)
        tarfile = files.split()[-1]
        s3_tarfile = tarfile + global_append
        localcap = local('aws s3 cp ' + tarfile + ' s3://' + s3bucket + '/dcp/' + s3_tarfile, capture=True)
        rootLogger.debug(localcap)
        rootLogger.debug(localcap.stderr)
        agfi_afi_ids = local("""aws ec2 create-fpga-image --input-storage-location Bucket={},Key={} --logs-storage-location Bucket={},Key={} --name "{}" --description "{}" """.format(s3bucket, "dcp/" + s3_tarfile, s3bucket, "logs/", afiname, description), capture=True)
        rootLogger.debug(agfi_afi_ids)
        rootLogger.debug(agfi_afi_ids.stderr)
        rootLogger.debug("create-fpga-image result: " + str(agfi_afi_ids))
        ids_as_dict = json.loads(agfi_afi_ids)
        agfi = ids_as_dict["FpgaImageGlobalId"]
        afi = ids_as_dict["FpgaImageId"]
        rootLogger.info("Resulting AGFI: " + str(agfi))
        rootLogger.info("Resulting AFI: " + str(afi))

    if global_build_config.distributed_elaboration:
        # if we generated verilog (and driver headers) remotely, we now need to build the driver locally
        rootLogger.info("Building FireSim-f1 driver")
        build_driver(global_build_config, buildconfig)

    rootLogger.info("Waiting for create-fpga-image completion.")
    checkstate = "pending"
    with lcd(pjoin(ddir, "results-build", results_builddir)), StreamLogger('stdout'), StreamLogger('stderr'):
        while checkstate == "pending":
            imagestate = local("""aws ec2 describe-fpga-images --fpga-image-id {} | tee AGFI_INFO""".format(afi), capture=True)
            state_as_dict = json.loads(imagestate)
            checkstate = state_as_dict["FpgaImages"][0]["State"]["Code"]
            rootLogger.info("Current state: " + str(checkstate))
            time.sleep(10)


    if checkstate == "available":
        # copy the image to all regions for the current user
        copy_afi_to_all_regions(afi)

        message_title = "FireSim FPGA Build Completed"
        agfi_entry = "[" + afiname + "]\nagfi=" + agfi + "\ndeploytripletoverride=None\ncustomruntimeconfig=None\n\n"
        message_body = "Your AGFI has been created!\nAdd\n" + agfi_entry + "\nto your config_hwdb.ini to use this hardware configuration."

        send_firesim_notification(message_title, message_body)

        rootLogger.info(message_title)
        rootLogger.info(message_body)

        # for convenience when generating a bunch of images. you can just
        # cat all the files in this directory after your builds finish to get
        # all the entries to copy into config_hwdb.ini
        hwdb_entry_file_location = """{}/built-hwdb-entries/""".format(ddir)
        local("mkdir -p " + hwdb_entry_file_location)
        with open(hwdb_entry_file_location + "/" + afiname, "w") as outputfile:
            outputfile.write(agfi_entry)

        if global_build_config.post_build_hook:
            with StreamLogger('stdout'), StreamLogger('stderr'):
                localcap = local("""{} {}""".format(global_build_config.post_build_hook,
                                                    pjoin(ddir, "results-build", results_builddir),
                                                    capture=True))
                rootLogger.debug("[localhost] " + str(localcap))
                rootLogger.debug("[localhost] " + str(localcap.stderr))

        rootLogger.info("Build complete! AFI ready. See {}.".format(pjoin(hwdb_entry_file_location,afiname)))
        return True
    else:
        return
