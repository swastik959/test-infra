#!/usr/bin/env python

# Copyright 2016 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Need to figure out why this only fails on travis
# pylint: disable=bad-continuation

"""Bootstraps starting a test job.

The following should already be done:
  git checkout http://k8s.io/test-infra
  cd $WORKSPACE
  test-infra/jenkins/bootstrap.py <--repo=R || --bare> <--job=J> <--pull=P || --branch=B>

The bootstrapper now does the following:
  # Note start time
  # read test-infra/jenkins/$JOB.json
  # check out repoes defined in $JOB.json
  # note job started
  # call runner defined in $JOB.json
  # upload artifacts (this will change later)
  # upload build-log.txt
  # note job ended

The contract with the runner is as follows:
  * Runner must exit non-zero if job fails for any reason.
"""


import argparse
import contextlib
import json
import logging
import os
import pipes
import random
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

ORIG_CWD = os.getcwd()  # Checkout changes cwd


def read_all(end, stream, append):
    """Read all buffered lines from a stream."""
    while not end or time.time() < end:
        line = stream.readline()
        if not line:
            return True  # Read everything
        # Strip \n at the end if any. Last line of file may not have one.
        append(line.rstrip('\n'))
        # Is there more on the buffer?
        ret = select.select([stream.fileno()], [], [], 0.1)
        if not ret[0]:
            return False  # Cleared buffer but not at the end
    return False  # Time expired


def elapsed(since):
    """Return the number of minutes elapsed since a time."""
    return (time.time() - since) / 60


def terminate(end, proc, kill):
    """Terminate or kill the process after end."""
    if not end or time.time() <= end:
        return False
    if kill:  # Process will not die, kill everything
        pgid = os.getpgid(proc.pid)
        logging.info(
            'Kill %d and process group %d', proc.pid, pgid)
        os.killpg(pgid, signal.SIGKILL)
        proc.kill()
        return True
    logging.info(
        'Terminate %d on timeout', proc.pid)
    proc.terminate()
    return True


def _call(end, cmd, stdin=None, check=True, output=None, log_failures=True):
    """Start a subprocess."""
    logging.info('Call:  %s', ' '.join(pipes.quote(c) for c in cmd))
    begin = time.time()
    if end:
        end = max(end, time.time() + 60)  # Allow at least 60s per command
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    if stdin:
        proc.stdin.write(stdin)
        proc.stdin.close()
    out = []
    code = None
    timeout = False
    reads = {
        proc.stderr.fileno(): (proc.stderr, logging.warning),
        proc.stdout.fileno(): (
            proc.stdout, (out.append if output else logging.info)),
    }
    while reads:
        if terminate(end, proc, timeout):
            if timeout:  # We killed everything
                break
            # Give subprocess some cleanup time before killing.
            end = time.time() + 15 * 60
            timeout = True
        ret = select.select(reads, [], [], 0.1)
        for fdesc in ret[0]:
            if read_all(end, *reads[fdesc]):
                reads.pop(fdesc)
        if not ret[0] and proc.poll() is not None:
            break  # process exited without closing pipes (timeout?)

    code = proc.wait()
    if timeout:
        code = code or 124
        logging.error('Build timed out')
    if code and log_failures:
        logging.error('Command failed')
    logging.info(
        'process %d exited with code %d after %.1fm',
        proc.pid, code, elapsed(begin))
    out.append('')
    lines = output and '\n'.join(out)
    if check and code:
        raise subprocess.CalledProcessError(code, cmd, lines)
    return lines


def ref_has_shas(ref):
    """Determine if a reference specifies shas (contains ':')"""
    return isinstance(ref, basestring) and ':' in ref


def pull_numbers(pull):
    """Turn a pull reference list into a list of PR numbers to merge."""
    if ref_has_shas(pull):
        return [r.split(':')[0] for r in pull.split(',')][1:]
    return [str(pull)]


def pull_ref(pull):
    """Turn a PR number of list of refs into specific refs to fetch and check out."""
    if isinstance(pull, int) or ',' not in pull:
        return ['+refs/pull/%d/merge' % int(pull)], ['FETCH_HEAD']
    pulls = pull.split(',')
    refs = []
    checkouts = []
    for ref in pulls:
        if ':' in ref:  # master:abcd or 1234:abcd
            name, sha = ref.split(':')
        elif not refs:  # master
            name, sha = ref, 'FETCH_HEAD'
        else:
            name = ref
            sha = 'refs/pr/%s' % ref

        checkouts.append(sha)
        if not refs:  # First ref should be branch to merge into
            refs.append(name)
        else:  # Subsequent refs should be PR numbers
            num = int(name)
            refs.append('+refs/pull/%d/head:refs/pr/%d' % (num, num))
    return refs, checkouts


def branch_ref(branch):
    """Split branch:sha if necessary."""
    if ref_has_shas(branch):
        split_refs = branch.split(':')
        return [split_refs[0]], [split_refs[1]]
    return [branch], ['FETCH_HEAD']


def repository(repo, ssh):
    """Return the url associated with the repo."""
    if repo.startswith('k8s.io/'):
        repo = 'github.com/kubernetes/%s' % (repo[len('k8s.io/'):])
    if ssh:
        if ":" not in repo:
            parts = repo.split('/', 1)
            repo = '%s:%s' % (parts[0], parts[1])
        return 'git@%s' % repo
    return 'https://%s' % repo


def random_sleep(attempt):
    """Sleep 2**attempt seconds with a random fractional offset."""
    time.sleep(random.random() + attempt ** 2)


def checkout(call, repo, branch, pull, ssh='', git_cache='', clean=False):
    """Fetch and checkout the repository at the specified branch/pull."""
    # pylint: disable=too-many-locals
    if bool(branch) == bool(pull):
        raise ValueError('Must specify one of --branch or --pull')

    if pull:
        refs, checkouts = pull_ref(pull)
    else:
        refs, checkouts = branch_ref(branch)

    git = 'git'
    if git_cache:
        cache_dir = '%s/%s' % (git_cache, repo)
        try:
            os.makedirs(cache_dir)
        except OSError:
            pass
        call([git, 'init', repo, '--separate-git-dir=%s' % cache_dir])
        call(['rm', '-f', '%s/index.lock' % cache_dir])
    else:
        call([git, 'init', repo])
    os.chdir(repo)

    if clean:
        call([git, 'clean', '-dfx'])
        call([git, 'reset', '--hard'])

    # To make a merge commit, a user needs to be set. It's okay to use a dummy
    # user here, since we're not exporting the history.
    call([git, 'config', '--local', 'user.name', 'K8S Bootstrap'])
    call([git, 'config', '--local', 'user.email', 'k8s_bootstrap@localhost'])
    retries = 3
    for attempt in range(retries):
        try:
            call([git, 'fetch', '--quiet', '--tags', repository(repo, ssh)] + refs)
            break
        except subprocess.CalledProcessError as cpe:
            if attempt >= retries - 1:
                raise
            if cpe.returncode != 128:
                raise
            logging.warning('git fetch failed')
            random_sleep(attempt)
    call([git, 'checkout', '-B', 'test', checkouts[0]])
    for ref, head in zip(refs, checkouts)[1:]:
        call(['git', 'merge', '--no-ff', '-m', 'Merge %s' % ref, head])


def repos_dict(repos):
    """Returns {"repo1": "branch", "repo2": "pull"}."""
    return {r: b or p for (r, (b, p)) in repos.items()}


def start(gsutil, paths, stamp, node_name, version, repos):
    """Construct and upload started.json."""
    data = {
        'timestamp': int(stamp),
        'jenkins-node': node_name,
        'node': node_name,
    }
    if version:
        data['repo-version'] = version
        data['version'] = version  # TODO(fejta): retire
    if repos:
        pull = repos[repos.main]
        if ref_has_shas(pull[1]):
            data['pull'] = pull[1]
        data['repos'] = repos_dict(repos)

    gsutil.upload_json(paths.started, data)
    # Upload a link to the build path in the directory
    if paths.pr_build_link:
        gsutil.upload_text(paths.pr_build_link, paths.pr_path)


class GSUtil(object):
    """A helper class for making gsutil commands."""
    gsutil = 'gsutil'

    def __init__(self, call):
        self.call = call

    def stat(self, path):
        """Return metadata about the object, such as generation."""
        cmd = [self.gsutil, 'stat', path]
        return self.call(cmd, output=True, log_failures=False)

    def ls(self, path):
        """List a bucket or subdir."""
        cmd = [self.gsutil, 'ls', path]
        return self.call(cmd, output=True)

    def upload_json(self, path, jdict, generation=None):
        """Upload the dictionary object to path."""
        if generation is not None:  # generation==0 means object does not exist
            gen = ['-h', 'x-goog-if-generation-match:%s' % generation]
        else:
            gen = []
        cmd = [
            self.gsutil, '-q',
            '-h', 'Content-Type:application/json'] + gen + [
            'cp', '-', path]
        self.call(cmd, stdin=json.dumps(jdict, indent=2))

    def copy_file(self, dest, orig):
        """Copy the file to the specified path using compressed encoding."""
        cmd = [self.gsutil, '-q', 'cp', '-Z', orig, dest]
        self.call(cmd)

    def upload_text(self, path, txt, cached=True):
        """Copy the text to path, optionally disabling caching."""
        headers = ['-h', 'Content-Type:text/plain']
        if not cached:
            headers += ['-h', 'Cache-Control:private, max-age=0, no-transform']
        cmd = [self.gsutil, '-q'] + headers + ['cp', '-', path]
        self.call(cmd, stdin=txt)

    def cat(self, path, generation):
        """Return contents of path#generation"""
        cmd = [self.gsutil, '-q', 'cat', '%s#%s' % (path, generation)]
        return self.call(cmd, output=True)


    def upload_artifacts(self, gsutil, path, artifacts):
        """Upload artifacts to the specified path."""
        # Upload artifacts
        if not os.path.isdir(artifacts):
            return
        try:
            # If remote path exists, it will create .../_artifacts subdir instead
            gsutil.ls(path)
            # Success means remote path exists
            remote_base = os.path.basename(path)
            local_base = os.path.basename(artifacts)
            if remote_base != local_base:
                # if basename are different, need to copy things over first.
                localpath = artifacts.replace(local_base, remote_base)
                os.rename(artifacts, localpath)
                artifacts = localpath
            path = path[:-len(remote_base + '/')]
        except subprocess.CalledProcessError:
            logging.warning('Remote dir %s not exist yet', path)
        cmd = [
            self.gsutil, '-m', '-q',
            '-o', 'GSUtil:use_magicfile=True',
            'cp', '-r', '-c', '-z', 'log,txt,xml',
            artifacts, path,
        ]
        self.call(cmd)


def append_result(gsutil, path, build, version, passed):
    """Download a json list and append metadata about this build to it."""
    # TODO(fejta): delete the clone of this logic in upload-to-gcs.sh
    #                  (this is update_job_result_cache)
    end = time.time() + 300  # try for up to five minutes
    errors = 0
    while time.time() < end:
        if errors:
            random_sleep(min(errors, 3))
        try:
            out = gsutil.stat(path)
            gen = re.search(r'Generation:\s+(\d+)', out).group(1)
        except subprocess.CalledProcessError:
            gen = 0
        if gen:
            try:
                cache = json.loads(gsutil.cat(path, gen))
                if not isinstance(cache, list):
                    raise ValueError(cache)
            except ValueError as exc:
                logging.warning('Failed to decode JSON: %s', exc)
                cache = []
            except subprocess.CalledProcessError:  # gen doesn't exist
                errors += 1
                continue
        else:
            cache = []
        cache.append({
            'version': version,  # TODO(fejta): retire
            'job-version': version,
            'buildnumber': build,
            'passed': bool(passed),
            'result': 'SUCCESS' if passed else 'FAILURE',
        })
        cache = cache[-300:]
        try:
            gsutil.upload_json(path, cache, generation=gen)
            return
        except subprocess.CalledProcessError:
            logging.warning('Failed to append to %s#%s', path, gen)
        errors += 1


def metadata(repos, artifacts, call):
    """Return metadata associated for the build, including inside artifacts."""
    path = os.path.join(artifacts or '', 'metadata.json')
    meta = None
    if os.path.isfile(path):
        try:
            with open(path) as fp:
                meta = json.loads(fp.read())
        except (IOError, ValueError):
            pass

    if not meta or not isinstance(meta, dict):
        meta = {}
    if repos:
        meta['repo'] = repos.main
        meta['repos'] = repos_dict(repos)

    try:
        commit = call(['git', 'rev-parse', 'HEAD'], output=True)
        if commit:
            meta['repo-commit'] = commit.strip()
    except subprocess.CalledProcessError:
        pass

    cwd = os.getcwd()
    os.chdir(test_infra('.'))
    try:
        commit = call(['git', 'rev-parse', 'HEAD'], output=True)
        if commit:
            meta['infra-commit'] = commit.strip()[:9]
    except subprocess.CalledProcessError:
        pass
    os.chdir(cwd)

    return meta


def finish(gsutil, paths, success, artifacts, build, version, repos, call):
    """
    Args:
        paths: a Paths instance.
        success: the build passed if true.
        artifacts: a dir containing artifacts to upload.
        build: identifier of this build.
        version: identifies what version of the code the build tested.
        repo: the target repo
    """

    if os.path.isdir(artifacts) and any(f for _, _, f in os.walk(artifacts)):
        try:
            gsutil.upload_artifacts(gsutil, paths.artifacts, artifacts)
        except subprocess.CalledProcessError:
            logging.warning('Failed to upload artifacts')

    meta = metadata(repos, artifacts, call)
    if not version:
        version = meta.get('job-version')
    if not version:  # TODO(fejta): retire
        version = meta.get('version')
    # github.com/kubernetes/release/find_green_build depends on append_result()
    # TODO(fejta): reconsider whether this is how we want to solve this problem.
    append_result(gsutil, paths.result_cache, build, version, success)
    if paths.pr_result_cache:
        append_result(gsutil, paths.pr_result_cache, build, version, success)

    data = {
        # TODO(fejta): update utils.go in contrib to accept a float
        'timestamp': int(time.time()),
        'result': 'SUCCESS' if success else 'FAILURE',
        'passed': bool(success),
        'metadata': meta,
    }
    if version:
        data['job-version'] = version
        data['version'] = version  # TODO(fejta): retire
    gsutil.upload_json(paths.finished, data)

    # Upload the latest build for the job.
    # Do this last, since other tools expect the rest of the data to be
    # published when this file is created.
    for path in {paths.latest, paths.pr_latest}:
        if path:
            try:
                gsutil.upload_text(path, str(build), cached=False)
            except subprocess.CalledProcessError:
                logging.warning('Failed to update %s', path)


def test_infra(*paths):
    """Return path relative to root of test-infra repo."""
    return os.path.join(ORIG_CWD, os.path.dirname(__file__), '..', *paths)


def node():
    """Return the name of the node running the build."""
    # TODO(fejta): jenkins sets the node name and our infra expect this value.
    # TODO(fejta): Consider doing something different here.
    if NODE_ENV not in os.environ:
        os.environ[NODE_ENV] = ''.join(socket.gethostname().split('.')[:1])
    return os.environ[NODE_ENV]


def find_version(call):
    """Determine and return the version of the build."""
    # TODO(fejta): once job-version is functional switch this to
    # git rev-parse [--short=N] HEAD^{commit}
    version_file = 'version'
    if os.path.isfile(version_file):
        # e2e tests which download kubernetes use this path:
        with open(version_file) as fp:
            return fp.read().strip()

    version_script = 'hack/lib/version.sh'
    if os.path.isfile(version_script):
        cmd = [
            'bash', '-c', (
"""
set -o errexit
set -o nounset
export KUBE_ROOT=.
source %s
kube::version::get_version_vars
echo $KUBE_GIT_VERSION
""" % version_script)
        ]
        return call(cmd, output=True).strip()

    return 'unknown'


class Paths(object):  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """Links to remote gcs-paths for uploading results."""
    def __init__(  # pylint: disable=too-many-arguments
        self,
        artifacts,  # artifacts folder (in build)
        build_log,  # build-log.txt (in build)
        pr_path,  # path to build
        finished,  # finished.json (metadata from end of build)
        latest,   # latest-build.txt (in job)
        pr_build_link,  # file containng pr_path (in job directory)
        pr_latest,  # latest-build.txt (in pr job)
        pr_result_cache,  # jobResultsCache.json (in pr job)
        result_cache,  # jobResultsCache.json (cache of latest results in job)
        started,  # started.json  (metadata from start of build)
    ):
        self.artifacts = artifacts
        self.build_log = build_log
        self.pr_path = pr_path
        self.finished = finished
        self.latest = latest
        self.pr_build_link = pr_build_link
        self.pr_latest = pr_latest
        self.pr_result_cache = pr_result_cache
        self.result_cache = result_cache
        self.started = started



def ci_paths(base, job, build):
    """Return a Paths() instance for a continuous build."""
    latest = os.path.join(base, job, 'latest-build.txt')
    return Paths(
        artifacts=os.path.join(base, job, build, 'artifacts'),
        build_log=os.path.join(base, job, build, 'build-log.txt'),
        pr_path=None,
        finished=os.path.join(base, job, build, 'finished.json'),
        latest=latest,
        pr_build_link=None,
        pr_latest=None,
        pr_result_cache=None,
        result_cache=os.path.join(base, job, 'jobResultsCache.json'),
        started=os.path.join(base, job, build, 'started.json'),
    )



def pr_paths(base, repos, job, build):
    """Return a Paths() instance for a PR."""
    if not repos:
        raise ValueError('repos is empty')
    repo = repos.main
    pull = str(repos[repo][1])
    if repo in ['k8s.io/kubernetes', 'kubernetes/kubernetes']:
        prefix = ''
    elif repo.startswith('k8s.io/'):
        prefix = repo[len('k8s.io/'):]
    elif repo.startswith('kubernetes/'):
        prefix = repo[len('kubernetes/'):]
    elif repo.startswith('github.com/'):
        prefix = repo[len('github.com/'):].replace('/', '_')
    else:
        prefix = repo.replace('/', '_')
    # Batch merges are those with more than one PR specified.
    pr_nums = pull_numbers(pull)
    if len(pr_nums) > 1:
        pull = os.path.join(prefix, 'batch')
    else:
        pull = os.path.join(prefix, pr_nums[0])
    pr_path = os.path.join(base, 'pull', pull, job, build)
    result_cache = os.path.join(
            base, 'directory', job, 'jobResultsCache.json')
    pr_result_cache = os.path.join(
            base, 'pull', pull, job, 'jobResultsCache.json')
    return Paths(
        artifacts=os.path.join(pr_path, 'artifacts'),
        build_log=os.path.join(pr_path, 'build-log.txt'),
        pr_path=pr_path,
        finished=os.path.join(pr_path, 'finished.json'),
        latest=os.path.join(base, 'directory', job, 'latest-build.txt'),
        pr_build_link=os.path.join(base, 'directory', job, '%s.txt' % build),
        pr_latest=os.path.join(base, 'pull', pull, job, 'latest-build.txt'),
        pr_result_cache=pr_result_cache,
        result_cache=result_cache,
        started=os.path.join(pr_path, 'started.json'),
    )



BUILD_ENV = 'BUILD_NUMBER'
BOOTSTRAP_ENV = 'BOOTSTRAP_MIGRATION'
CLOUDSDK_ENV = 'CLOUDSDK_CONFIG'
GCE_KEY_ENV = 'JENKINS_GCE_SSH_PRIVATE_KEY_FILE'
GUBERNATOR = 'https://k8s-gubernator.appspot.com/build'
HOME_ENV = 'HOME'
JOB_ENV = 'JOB_NAME'
NODE_ENV = 'NODE_NAME'
SERVICE_ACCOUNT_ENV = 'GOOGLE_APPLICATION_CREDENTIALS'
WORKSPACE_ENV = 'WORKSPACE'
GCS_ARTIFACTS_ENV = 'GCS_ARTIFACTS_DIR'


def build_name(started):
    """Return the unique(ish) string representing this build."""
    # TODO(fejta): right now jenkins sets the BUILD_NUMBER and does this
    #              in an environment variable. Consider migrating this to a
    #              bootstrap.py flag
    if BUILD_ENV not in os.environ:
        # Automatically generate a build number if none is set
        uniq = '%x-%d' % (hash(node()), os.getpid())
        autogen = time.strftime('%Y%m%d-%H%M%S-' + uniq, time.gmtime(started))
        os.environ[BUILD_ENV] = autogen
    return os.environ[BUILD_ENV]


def setup_credentials(call, robot, upload):
    """Activate the service account unless robot is none."""
    # TODO(fejta): stop activating inside the image
    # TODO(fejta): allow use of existing gcloud auth
    if robot:
        os.environ[SERVICE_ACCOUNT_ENV] = robot
    if not os.getenv(SERVICE_ACCOUNT_ENV) and upload:
        logging.warning('Cannot --upload=%s, no active gcloud account.', upload)
        raise ValueError('--upload requires --service-account')
    if not os.getenv(SERVICE_ACCOUNT_ENV) and not upload:
        logging.info('Will not upload results.')
        return
    if not os.path.isfile(os.environ[SERVICE_ACCOUNT_ENV]):
        raise IOError(
            'Cannot find service account credentials',
            os.environ[SERVICE_ACCOUNT_ENV],
            'Create service account and then create key at '
            'https://console.developers.google.com/iam-admin/serviceaccounts/project',  # pylint: disable=line-too-long
        )
    call([
        'gcloud',
        'auth',
        'activate-service-account',
        '--key-file=%s' % os.environ[SERVICE_ACCOUNT_ENV],
    ])
    try:  # Old versions of gcloud may not support this value
        account = call(
            ['gcloud', 'config', 'get-value', 'account'], output=True).strip()
    except subprocess.CalledProcessError:
        account = 'unknown'
    logging.info('Will upload results to %s using %s', upload, account)


def setup_logging(path):
    """Initialize logging to screen and path."""
    # See https://docs.python.org/2/library/logging.html#logrecord-attributes
    # [IWEF]mmdd HH:MM:SS.mmm] msg
    fmt = '%(levelname).1s%(asctime)s.%(msecs)03d] %(message)s'  # pylint: disable=line-too-long
    datefmt = '%m%d %H:%M:%S'
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
    )
    build_log = logging.FileHandler(filename=path, mode='w')
    build_log.setLevel(logging.INFO)
    formatter = logging.Formatter(fmt, datefmt=datefmt)
    build_log.setFormatter(formatter)
    logging.getLogger('').addHandler(build_log)
    return build_log


def setup_magic_environment(job):
    """Set magic environment variables scripts currently expect."""
    home = os.environ[HOME_ENV]
    # TODO(fejta): jenkins sets these values. Consider migrating to using
    #              a secret volume instead and passing the path to this volume
    #              into bootstrap.py as a flag.
    os.environ.setdefault(
        GCE_KEY_ENV,
        os.path.join(home, '.ssh/google_compute_engine'),
    )
    os.environ.setdefault(
        'JENKINS_GCE_SSH_PUBLIC_KEY_FILE',
        os.path.join(home, '.ssh/google_compute_engine.pub'),
    )
    os.environ.setdefault(
        'JENKINS_AWS_SSH_PRIVATE_KEY_FILE',
        os.path.join(home, '.ssh/kube_aws_rsa'),
    )
    os.environ.setdefault(
        'JENKINS_AWS_SSH_PUBLIC_KEY_FILE',
        os.path.join(home, '.ssh/kube_aws_rsa.pub'),
    )


    cwd = os.getcwd()
    # TODO(fejta): jenkins sets WORKSPACE and pieces of our infra expect this
    #              value. Consider doing something else in the future.
    os.environ[WORKSPACE_ENV] = cwd
    # TODO(fejta): Previously dockerized-e2e-runner.sh also sets HOME to WORKSPACE,
    #              probably to minimize leakage between jobs.
    #              Consider accomplishing this another way.
    os.environ[HOME_ENV] = cwd
    # TODO(fejta): jenkins sets JOB_ENV and pieces of our infra expect this
    #              value. Consider making everything below here agnostic to the
    #              job name.
    if JOB_ENV not in os.environ:
        os.environ[JOB_ENV] = job
    elif os.environ[JOB_ENV] != job:
        logging.warning('%s=%s (overrides %s)', JOB_ENV, job, os.environ[JOB_ENV])
        os.environ[JOB_ENV] = job
    # TODO(fejta): Magic value to tell our test code not do upload started.json
    # TODO(fejta): delete upload-to-gcs.sh and then this value.
    os.environ[BOOTSTRAP_ENV] = 'yes'
    # This helps prevent reuse of cloudsdk configuration. It also reduces the
    # risk that running a job on a workstation corrupts the user's config.
    os.environ[CLOUDSDK_ENV] = '%s/.config/gcloud' % cwd


def job_args(args):
    """Converts 'a ${FOO} $bar' into 'a wildly different string'."""
    return [os.path.expandvars(a) for a in args]


def job_script(job, use_json):
    """Return path to script for job."""
    if not use_json:
        return [test_infra('jobs/%s.sh' % job)]
    with open(test_infra('jobs/config.json')) as fp:
        config = json.loads(fp.read())
    job_config = config[job]
    cmd = test_infra('scenarios/%s.py' % job_config['scenario'])
    return [cmd] + job_args(job_config.get('args', []))




def gubernator_uri(paths):
    """Return a gubernator link for this build."""
    job = os.path.dirname(paths.build_log)
    if job.startswith('gs:/'):
        return job.replace('gs:/', GUBERNATOR, 1)
    return job


@contextlib.contextmanager
def choose_ssh_key(ssh):
    """Creates a script for GIT_SSH that uses -i ssh if set."""
    if not ssh:  # Nothing to do
        yield
        return

    # Create a script for use with GIT_SSH, which defines the program git uses
    # during git fetch. In the future change this to GIT_SSH_COMMAND
    # https://superuser.com/questions/232373/how-to-tell-git-which-private-key-to-use
    with tempfile.NamedTemporaryFile(prefix='ssh', delete=False) as fp:
        fp.write('#!/bin/sh\nssh -o StrictHostKeyChecking=no -i \'%s\' -F /dev/null "${@}"\n' % ssh)
    try:
        os.chmod(fp.name, 0500)
        had = 'GIT_SSH' in os.environ
        old = os.getenv('GIT_SSH')
        os.environ['GIT_SSH'] = fp.name

        yield

        del os.environ['GIT_SSH']
        if had:
            os.environ['GIT_SSH'] = old
    finally:
        os.unlink(fp.name)


def setup_root(call, root, repos, ssh, git_cache, clean):
    """Create root dir, checkout repo and cd into resulting dir."""
    if not os.path.exists(root):
        os.makedirs(root)
    root_dir = os.path.realpath(root)
    logging.info('Root: %s', root_dir)
    with choose_ssh_key(ssh):
        for repo, (branch, pull) in repos.items():
            os.chdir(root_dir)
            logging.info(
                'Checkout: %s %s',
                os.path.join(root_dir, repo),
                pull and pull or branch)
            checkout(call, repo, branch, pull, ssh, git_cache, clean)
    if len(repos) > 1:  # cd back into the primary repo
        os.chdir(root_dir)
        os.chdir(repos.main)


class Repos(dict):
    """{"repo": (branch, pull)} dict with a .main attribute."""
    main = ''

    def __setitem__(self, k, v):
        if not self:
            self.main = k
        return super(Repos, self).__setitem__(k, v)


def parse_repos(args):
    """Convert --repo=foo=this,123:abc,555:ddd into a Repos()."""
    repos = args.repo or {}
    if not repos and not args.bare:
        raise ValueError('--bare or --repo required')
    ret = Repos()
    if len(repos) != 1:
        if args.pull:
            raise ValueError('Multi --repo does not support --pull, use --repo=R=branch,p1,p2')
        if args.branch:
            raise ValueError('Multi --repo does not support --branch, use --repo=R=branch')
    elif len(repos) == 1 and (args.branch or args.pull):
        repo = repos[0]
        if '=' in repo or ':' in repo:
            raise ValueError('--repo cannot contain = or : with --branch or --pull')
        ret[repo] = (args.branch, args.pull)
        return ret
    for repo in repos:
        mat = re.match(r'([^=]+)(=([^:,~^\s]+(:[0-9a-fA-F]+)?(,|$))+)?$', repo)
        if not mat:
            raise ValueError('bad repo', repo, repos)
        this_repo = mat.group(1)
        if not mat.group(2):
            ret[this_repo] = ('master', '')
            continue
        commits = mat.group(2)[1:].split(',')
        if len(commits) == 1:
            # Checking out a branch, possibly at a specific commit
            ret[this_repo] = (commits[0], '')
            continue
        # Checking out one or more PRs
        ret[this_repo] = ('', ','.join(commits))
    return ret


def bootstrap(args):
    """Clone repo at pull/branch into root and run job script."""
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    job = args.job
    repos = parse_repos(args)
    upload = args.upload
    use_json = args.json


    build_log_path = os.path.abspath('build-log.txt')
    build_log = setup_logging(build_log_path)
    started = time.time()
    if args.timeout:
        end = started + args.timeout * 60
    else:
        end = 0
    call = lambda *a, **kw: _call(end, *a, **kw)
    gsutil = GSUtil(call)

    logging.info('Bootstrap %s...', job)
    build = build_name(started)

    if upload:
        if repos and repos[repos.main][1]:  # merging commits, a pr
            paths = pr_paths(upload, repos, job, build)
        else:
            paths = ci_paths(upload, job, build)
        logging.info('Gubernator results at %s', gubernator_uri(paths))
        # TODO(fejta): Replace env var below with a flag eventually.
        os.environ[GCS_ARTIFACTS_ENV] = paths.artifacts

    version = 'unknown'
    exc_type = None
    setup_creds = False

    try:
        setup_root(call, args.root, repos, args.ssh, args.git_cache, args.clean)
        logging.info('Configure environment...')
        if repos:
            version = find_version(call)
        else:
            version = ''
        setup_magic_environment(job)
        setup_credentials(call, args.service_account, upload)
        setup_creds = True
        logging.info('Start %s at %s...', build, version)
        if upload:
            start(gsutil, paths, started, node(), version, repos)
        success = False
        try:
            call(job_script(job, use_json))
            logging.info('PASS: %s', job)
            success = True
        except subprocess.CalledProcessError:
            logging.error('FAIL: %s', job)
    except Exception:  # pylint: disable=broad-except
        exc_type, exc_value, exc_traceback = sys.exc_info()
        logging.exception('unexpected error')
        success = False
    if not setup_creds:
        setup_credentials(call, args.service_account, upload)
    if upload:
        logging.info('Upload result and artifacts...')
        logging.info('Gubernator results at %s', gubernator_uri(paths))
        try:
            finish(gsutil, paths, success, '_artifacts', build, version, repos, call)
        except subprocess.CalledProcessError:  # Still try to upload build log
            success = False
    logging.getLogger('').removeHandler(build_log)
    build_log.close()
    if upload:
        gsutil.copy_file(paths.build_log, build_log_path)
    if exc_type:
        raise exc_type, exc_value, exc_traceback  # pylint: disable=raising-bad-type
    if not success:
        # TODO(fejta/spxtr): we should distinguish infra and non-infra problems
        # by exit code and automatically retrigger after an infra-problem.
        sys.exit(1)

def parse_args(arguments=None):
    """Parse arguments or sys.argv[1:]."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--json',
        nargs='?', const=1, default=0,
        type=int, help='--job is a json key, not a .sh')
    parser.add_argument('--root', default='.', help='Root dir to work with')
    parser.add_argument(
        '--timeout', type=float, default=0, help='Timeout in minutes if set')
    parser.add_argument(
        '--pull',
        help='Deprecated, use --repo=k8s.io/foo=master:abcd,12:ef12,45:ff65')
    parser.add_argument(
        '--branch',
        help='Deprecated, use --repo=k8s.io/foo=master')
    parser.add_argument(
        '--repo',
        action='append',
        help='Fetch the specified repositories, with the first one considered primary')
    parser.add_argument(
        '--bare',
        action='store_true',
        help='Do not check out a repository')
    parser.add_argument('--job', required=True, help='Name of the job to run')
    parser.add_argument(
        '--upload',
        help='Upload results here if set, requires --service-account')
    parser.add_argument(
        '--service-account',
        help='Activate and use path/to/service-account.json if set.')
    parser.add_argument(
        '--ssh',
        help='Use the ssh key to fetch the repository instead of https if set.')
    parser.add_argument(
        '--git-cache',
        help='Location of the git cache.')
    parser.add_argument(
        '--clean',
        action='store_true',
        help='Clean the git repo before running tests.')
    args = parser.parse_args(arguments)
    if bool(args.repo) == bool(args.bare):
        raise argparse.ArgumentTypeError(
            'Expected --repo xor --bare:', args.repo, args.bare)
    return args


if __name__ == '__main__':
    ARGS = parse_args()
    bootstrap(ARGS)
