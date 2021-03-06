"""Logic for provisioning and bootstrapping an automatically
created Cloudformation template.

The "stackname" parameter these functions take is the name of the cfn template
without the extension."""

import json
import os
from os.path import join
from functools import partial
from StringIO import StringIO
from . import core, utils, config, keypair
from .core import connect_aws_with_stack, stack_pem, stack_conn, project_data_for_stackname
from .utils import first
from .config import BOOTSTRAP_USER
from fabric.api import sudo, put
import fabric.exceptions as fabric_exceptions
from fabric.contrib import files
from boto.exception import BotoServerError
from kids.cache import cache as cached
from buildercore import cfngen

import logging
LOG = logging.getLogger(__name__)

#
# utils
#

def run_script(script_path, *script_params):
    """uploads a script for SCRIPTS_PATH and executes it in the /tmp dir with given params.
    ASSUMES YOU ARE CONNECTED TO A STACK"""
    local_script = join(config.SCRIPTS_PATH, script_path)
    remote_script = join('/tmp', os.path.basename(script_path))
    put(local_script, remote_script)
    cmd = ["/bin/bash", remote_script] + map(str, list(script_params))
    retval = sudo(" ".join(cmd))
    sudo("rm " + remote_script) # remove the script after executing it
    return retval

def prep_ec2_instance():
    """called after stack creation and before AMI creation"""
    return run_script("prep-stack.sh")


#
# provision stack
#

def _noop():
    pass

def create_stack(stackname):
    pdata = project_data_for_stackname(stackname)
    parameters = []
    on_start=_noop
    on_error=_noop
    if pdata['aws']['ec2']:
        parameters.append(('KeyName', stackname))
        on_start = lambda: keypair.create_keypair(stackname)
        on_error = lambda: keypair.delete_keypair(stackname)

    return _create_generic_stack(stackname, parameters, on_start, on_error)

def _create_generic_stack(stackname, parameters=[], on_start=_noop, on_error=_noop):
    "simply creates the stack of resources on AWS, talking to CloudFormation."
    LOG.info('creating stack %r', stackname)
    stack_body = core.stack_json(stackname)
    try:
        on_start()
        conn = connect_aws_with_stack(stackname, 'cfn')
        conn.create_stack(stackname, stack_body, parameters=parameters)
        _wait_for_stack_creation_to_complete(stackname)
        context = cfngen.context(stackname)
        # setup various resources after creation, where necessary
        setup_ec2(stackname, context['ec2'])
        setup_sqs(stackname, context['sqs'], context['project']['aws']['region'])

        return True
    except BotoServerError as err:
        if err.message.endswith(' already exists'):
            LOG.debug(err.message)
            return False
        LOG.exception("unhandled Boto exception attempting to create stack", extra={'stackname': stackname, 'parameters': parameters})
        on_error()
        raise
    except KeyboardInterrupt:
        LOG.debug("caught keyboard interrupt, cancelling...")
        return False
    except:
        LOG.exception("unhandled exception attempting to create stack", extra={'stackname': stackname})
        on_error()
        raise

def _wait_for_stack_creation_to_complete(stackname):
    def is_updating(stackname):
        return core.describe_stack(stackname).stack_status in ['CREATE_IN_PROGRESS']
    utils.call_while(partial(is_updating, stackname), update_msg='Waiting for AWS to finish creating stack ...')

def setup_ec2(stackname, context_ec2):
    if not context_ec2:
        return

    with stack_conn(stackname, username=BOOTSTRAP_USER):
        def is_resourcing():
            try:
                # we have an issue where the stack is created, however the security group
                # hasn't been attached or ssh isn't running yet and we can't get in.
                # this waits until a connection can be made and a file is found before continuing.
                # moreover, call until:
                # - bootstrap user exists and we can access it through SSH
                # - cloud-init has finished running
                #       otherwise we may be missing /etc/apt/source.list, which is generated on boot
                #       https://www.digitalocean.com/community/questions/how-to-make-sure-that-cloud-init-finished-running 
                return not files.exists(join('/home', BOOTSTRAP_USER, ".ssh/authorized_keys")) or not files.exists('/var/lib/cloud/instance/boot-finished')
            except fabric_exceptions.NetworkError:
                LOG.debug("failed to connect to server ...")
                return True
        utils.call_while(is_resourcing, interval=3, update_msg='Waiting for /home/ubuntu to be detected ...')
        prep_ec2_instance()


def setup_sqs(stackname, context_sqs, region):
    """
    Connects SQS queues created by Cloud Formation to SNS topics where 
    necessary, adding both the subscription and the IAM policy to let the SNS 
    topic write to the queue
    """
    assert isinstance(context_sqs, dict), ("Not a dictionary of queues pointing to their subscriptions: %s" % context_sqs)

    sqs = core.boto_sqs_conn(region)
    sns = core.boto_sns_conn(region)
    for queue_name in context_sqs:
        LOG.info('Setup of SQS queue %s', queue_name, extra={'stackname': stackname})
        queue = sqs.lookup(queue_name)
        subscriptions = context_sqs[queue_name]
        assert isinstance(subscriptions, list), ("Not a list of topics: %s" % subscriptions)
        for topic_name in subscriptions:
            LOG.info('Subscribing %s to SNS topic %s', queue_name, topic_name, extra={'stackname': stackname})
            # idempotent, works as lookup
            # risky, may subscribe to a typo-filled topic name like 'aarticles'
            # there is no boto method to lookup a topic
            topic_lookup = sns.create_topic(topic_name) 
            topic_arn = topic_lookup['CreateTopicResponse']['CreateTopicResult']['TopicArn']
            # deals with both subscription and IAM policy
            sns.subscribe_sqs_queue(topic_arn, queue)


#
#  attached stack resources, ec2 data
#

@core.requires_active_stack
def stack_resources(stackname):
    "returns a list of resources provisioned by the given stack"
    return connect_aws_with_stack(stackname, 'cfn').describe_stack_resources(stackname)

def ec2_instance_data(stackname):
    "returns the ec2 instance data from the first ec2 instance the stack has"
    assert stackname, "stackname must be valid, not None"
    ec2 = first([r for r in stack_resources(stackname) if r.resource_type == "AWS::EC2::Instance"])
    conn = connect_aws_with_stack(stackname, 'ec2')
    return conn.get_only_instances([ec2.physical_resource_id])[0]

@cached
def master_data(region):
    "returns the ec2 instance data for the master-server"
    stackname = core.find_master(region)
    assert stackname, ("Cannot find the master in region %s" % region)
    return ec2_instance_data(stackname)

def master(region, key):
    return getattr(master_data(region), key)

#
# bootstrap stack
#

@core.requires_active_stack
def template_info(stackname):
    "returns some useful information about the given stackname as a map"
    conn = connect_aws_with_stack(stackname, 'cfn')
    data = conn.describe_stacks(stackname)[0].__dict__
    data['outputs'] = reduce(utils.conj, map(lambda o: {o.key: o.value}, data['outputs']))
    return utils.exsubdict(data, ['connection', 'parameters'])

def write_environment_info(stackname, overwrite=False):
    """Looks for /etc/cfn-info.json and writes one if not found.
    Must be called with an active stack connection."""
    if not files.exists("/etc/cfn-info.json") or overwrite:
        LOG.info('no cfn outputs found or overwrite=True, writing /etc/cfn-info.json ...')
        infr_config = utils.json_dumps(template_info(stackname))
        return put(StringIO(infr_config), "/etc/cfn-info.json", use_sudo=True)
    LOG.debug('cfn outputs found, skipping')
    return []

#
#
#

@core.requires_active_stack
def update_stack(stackname):
    pdata = project_data_for_stackname(stackname)
    # TODO: only EC2 parts can be updated at the moment
    if pdata['aws']['ec2']:
        update_ec2_stack(stackname)

@core.requires_stack_file
def create_update(stackname):
    if not core.stack_is_active(stackname):
        print 'stack does not exist, creating'
        create_stack(stackname)
    print 'updating stack'
    update_stack(stackname)
    return stackname

def update_ec2_stack(stackname):
    """installs/updates the ec2 instance attached to the specified stackname.

    Once AWS has finished creating an EC2 instance for us, we need to install 
    Salt and get it talking to the master server. Salt comes with a bootstrap 
    script that can be downloaded from the web and then very conveniently 
    installs it's own dependencies. Once Salt is installed we give it an ID 
    (the given `stackname`), the address of the master server """
    pdata = project_data_for_stackname(stackname)
    region = pdata['aws']['region']
    is_master = core.is_master_server_stack(stackname)

    # forward-agent == ssh -A
    with stack_conn(stackname, username=BOOTSTRAP_USER, forward_agent=True):
        # upload private key if not present remotely
        if not files.exists("/root/.ssh/id_rsa", use_sudo=True):
            # if it also doesn't exist on the filesystem, die horribly.
            # regular updates shouldn't have to deal with this.
            pem = stack_pem(stackname, die_if_doesnt_exist=True)
            put(pem, "/root/.ssh/id_rsa", use_sudo=True)

        # write out environment config so Salt can read CFN outputs
        write_environment_info(stackname)

        salt_version = pdata['salt']
        install_master_flag = str(is_master).lower() # ll: 'true' 
        master_ip = master(region, 'private_ip_address')

        run_script('bootstrap.sh', salt_version, stackname, install_master_flag, master_ip)
        if is_master:
            builder_private_repo = pdata['private-repo']
            run_script('init-master.sh', stackname, builder_private_repo)
            run_script('update-master.sh', stackname, builder_private_repo)

        # this will tell the machine to update itself
        run_script('highstate.sh')

@core.requires_stack_file
def delete_stack_file(stackname):
    try:
        core.describe_stack(stackname) # triggers exception if NOT exists
        LOG.warning('stack %r still exists, refusing to delete stack files. delete active stack first.', stackname)
        return
    except BotoServerError, ex:
        if not ex.message.endswith('does not exist'):
            LOG.exception("unhandled exception attempting to confirm if stack %r exists", stackname)
            raise
    ext_list = [
        ".pem",
        ".pub",
        ".json",
        ".yaml", # yaml files are now deprecated
    ]
    paths = [join(config.STACK_DIR, stackname + ext) for ext in ext_list]
    paths = filter(os.path.exists, paths)
    def _unlink(path):
        os.unlink(path)
        return not os.path.exists(path)
    return dict(zip(paths, map(_unlink, paths)))

def delete_stack(stackname):
    try:
        connect_aws_with_stack(stackname, 'cfn').delete_stack(stackname)
        def is_deleting(stackname):
            try:
                return core.describe_stack(stackname).stack_status in ['DELETE_IN_PROGRESS']
            except BotoServerError as err:
                if err.message.endswith('does not exist'):
                    return False
                raise # not sure what happened, but we're not handling it here. die.
        utils.call_while(partial(is_deleting, stackname), update_msg='Waiting for AWS to finish deleting stack ...')
        keypair.delete_keypair(stackname)
        delete_stack_file(stackname)
        LOG.info("stack %r deleted", stackname)
    except BotoServerError as err:
        LOG.exception("[%s: %s] %s (request-id: %s)", err.status, err.reason, err.message, err.request_id)
