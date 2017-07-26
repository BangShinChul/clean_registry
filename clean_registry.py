#!/usr/bin/env python3
#
# This script purges untagged repositories and runs the garbage collector in Docker Registry >= 2.4.0.
# It works on the whole registry or the specified repositories.
# The optional flag -x may be used to completely remove the specified repositories or tagged images.
#
# NOTES:
#   - This script stops the Registry container during cleanup to prevent corruption,
#     making it temporarily unavailable to clients.
#   - This script assumes local storage (the filesystem storage driver).
#   - This script may run standalone or dockerized.
#   - This script is Python 3 only.
#
# v1.0 by Ricardo Branco
#
# MIT License
#

import os
import re
import sys
import tarfile
import subprocess

from argparse import ArgumentParser
from distutils.version import LooseVersion
from glob import iglob
from io import BytesIO
from shutil import rmtree
from requests import exceptions
from docker.errors import APIError, NotFound

try:
    import docker
except ImportError:
    error("Please install docker-py with: pip3 install docker")

try:
    import yaml
except ImportError:
    error("Please install PyYaml with: pip3 install pyyaml")

VERSION = "1.0"


def dockerized():
    '''Returns True if we're inside a Docker container, False otherwise.'''
    return os.path.isfile("/.dockerenv")


def error(msg, Exit=True):
    '''Prints an error message and optionally exit with a status code of 1'''
    print("ERROR: " + str(msg), file=sys.stderr)
    if Exit:
        sys.exit(1)


def remove(path):
    '''Run rmtree() in verbose mode'''
    rmtree(path)
    if not args.quiet:
        print("removed directory " + path)


def clean_revisions(repo):
    '''Remove the revision manifests that are not present in the tags directory'''
    revisions = set(os.listdir(repo + "/_manifests/revisions/sha256/"))
    manifests = set(map(os.path.basename, iglob(repo + "/_manifests/tags/*/*/sha256/*")))
    revisions.difference_update(manifests)
    for revision in revisions:
        remove(repo + "/_manifests/revisions/sha256/" + revision)


def clean_tag(repo, tag):
    '''Clean a specific repo:tag'''
    link = repo + "/_manifests/tags/" + tag + "/current/link"
    if not os.path.isfile(link):
        error("No such tag: %s in repository %s" % (tag, repo), Exit=False)
        return False
    if args.remove:
        remove(repo + "/_manifests/tags/" + tag)
    else:
        with open(link) as f:
            current = f.read()[len("sha256:"):]
        path = repo + "/_manifests/tags/" + tag + "/index/sha256/"
        for index in os.listdir(path):
            if index == current:
                continue
            remove(path + index)
        clean_revisions(repo)
    return True


def clean_repo(image):
    '''Clean all tags (or a specific one, if specified) from a specific repository'''
    repo, tag = image.split(":", 1) if ":" in image else (image, "")

    if not os.path.isdir(repo):
        error("No such repository: " + repo, Exit=False)
        return False

    if args.remove:
        tags = os.listdir(repo + "/_manifests/tags/")
        if not tag or len(tags) == 1 and tag in tags:
            remove(repo)
            return True

    if tag:
        return clean_tag(repo, tag)

    currents = set()
    for link in iglob(repo + "/_manifests/tags/*/current/link"):
        with open(link) as f:
            currents.add(f.read()[len("sha256:"):])
    for index in iglob(repo + "/_manifests/tags/*/index/sha256/*"):
        if os.path.basename(index) not in currents:
            remove(index)

    clean_revisions(repo)
    return True


def check_name(image):
    '''Checks the whole repository:tag name'''
    repo, tag = image.split(":", 1) if ":" in image else (image, "latest")

    # From https://github.com/moby/moby/blob/master/image/spec/v1.2.md
    # Tag values are limited to the set of characters [a-zA-Z0-9_.-], except they may not start with a . or - character.
    # Tags are limited to 128 characters.
    #
    # From https://github.com/docker/distribution/blob/master/docs/spec/api.md
    # 1. A repository name is broken up into path components. A component of a repository name must be at least
    #    one lowercase, alpha-numeric characters, optionally separated by periods, dashes or underscores.
    #    More strictly, it must match the regular expression [a-z0-9]+(?:[._-][a-z0-9]+)*
    # 2. If a repository name has two or more path components, they must be separated by a forward slash ("/").
    # 3. The total length of a repository name, including slashes, must be less than 256 characters.

    # Note: Internally, distribution permits multiple dashes and up to 2 underscores as separators.
    # See https://github.com/docker/distribution/blob/master/reference/regexp.go

    return len(image) < 256 and len(tag) < 129 and re.match('[a-zA-Z0-9_][a-zA-Z0-9_.-]*$', tag) and \
           all(re.match('[a-z0-9]+(?:(?:[._]|__|[-]*)[a-z0-9]+)*$', path) for path in repo.split("/"))


class RegistryCleaner():
    '''Simple callable class for Docker Registry cleaning duties'''
    def __init__(self, container_name):
        self.docker = docker.from_env()

        try:
            self.info = self.docker.api.inspect_container(container_name)
            self.container = self.info['Id']
        except (APIError, exceptions.ConnectionError) as err:
            error(str(err))

        if self.info['Config']['Image'] != "registry:2":
            error("The container %s is not running the registry:2 image" % (container_name))

        if LooseVersion(self.get_image_version()) < LooseVersion("v2.4.0"):
            error("You're not running Docker Registry 2.4.0+")

        self.registry_dir = self.get_registry_dir()
        try:
            os.chdir(self.registry_dir + "/docker/registry/v2/repositories")
        except FileNotFoundError as err:
            error(err)

        if dockerized() and not os.getenv("REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"):
            os.environ['REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY'] = self.registry_dir

    def __call__(self):
        self.docker.api.stop(self.container)

        images = args.images if args.images else os.listdir(".")

        rc = 0
        for image in images:
            if not clean_repo(image):
                rc = 1

        if not self.garbage_collect():
            rc = 1

        self.docker.api.start(self.container)
        return rc

    def get_file(self, filename):
        '''Returns the contents of the specified file from the container'''
        try:
            with self.docker.api.get_archive(self.container, filename)[0] as tar_stream:
                with BytesIO(tar_stream.data) as buf:
                    with tarfile.open(fileobj=buf) as tarf:
                        with tarf.extractfile(os.path.basename(filename)) as f:
                            data = f.read()
        except NotFound as err:
            error(err)
        return data

    def get_registry_dir(self):
        '''Gets the Registry directory'''
        registry_dir = os.getenv("REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY")
        if registry_dir:
            return registry_dir

        registry_dir = ""
        for env in self.info['Config']['Env']:
            var, value = env.split("=", 1)
            if var == "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY":
                registry_dir = value
                break

        if not registry_dir:
            config_yml = self.info['Args'][0]
            data = yaml.load(self.get_file(config_yml))
            try:
                registry_dir = data['storage']['filesystem']['rootdirectory']
            except KeyError:
                error("Unsupported storage driver")

        if dockerized():
            return registry_dir

        for item in self.info['Mounts']:
            if item['Destination'] == registry_dir:
                return item['Source']

    def get_image_version(self):
        '''Gets the Docker distribution version running on the container'''
        if self.info['State']['Running']:
            data = self.docker.containers.get(self.container).exec_run("/bin/registry --version").decode('utf-8')
        else:
            data = self.docker.containers.run(self.info["Image"], command="--version", remove=True).decode('utf-8')
        return data.split()[2]

    def garbage_collect(self):
        '''Runs garbage-collect'''
        command = "garbage-collect " + "/etc/docker/registry/config.yml"
        if dockerized():
            command = "/bin/registry " + command
            with subprocess.Popen(command.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                if not args.quiet:
                    print(proc.stdout.read().decode('utf-8'))
            status = proc.wait()
        else:
            cli = self.docker.containers.run("registry:2", command=command, detach=True,
                                             volumes={self.registry_dir: {'bind': "/var/lib/registry", 'mode': "rw"}})
            if not args.quiet:
                for line in cli.logs(stream=True):
                    print(line.decode('utf-8'), end="")
            status = True if cli.wait() == 0 else False
            cli.remove()
        return status


def main():
    '''Main function'''
    progname = os.path.basename(sys.argv[0])
    usage = "\rUsage: " + progname + " [OPTIONS] CONTAINER [REPOSITORY[:TAG]]..." + """
Options:
        -x, --remove    Remove the specified images or repositories.
        -q, --quiet     Supress non-error messages.
        -V, --version   Show version and exit."""

    parser = ArgumentParser(usage=usage, add_help=False)
    parser.add_argument('-h', '--help', action='store_true')
    parser.add_argument('-q', '--quiet', action='store_true')
    parser.add_argument('-x', '--remove', action='store_true')
    parser.add_argument('-V', '--version', action='store_true')
    parser.add_argument('container')
    parser.add_argument('images', nargs='*')
    global args
    args = parser.parse_args()

    if args.help:
        print('usage: ' + usage)
        sys.exit(0)
    elif args.version:
        print(progname + " " + VERSION)
        sys.exit(0)

    for image in args.images:
        if not check_name(image):
            error("Invalid Docker repository/tag: " + image)

    if args.remove and not args.images:
        error("The -x option requires that you specify at least one repository...")

    rc = RegistryCleaner(args.container)
    sys.exit(rc())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
