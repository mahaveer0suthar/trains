import abc
import os
from subprocess import call, CalledProcessError

import attr
import six
from pathlib2 import Path

from ....config.defs import (
    VCS_REPO_TYPE,
    VCS_DIFF,
    VCS_STATUS,
    VCS_ROOT,
    VCS_BRANCH,
    VCS_COMMIT_ID,
    VCS_REPOSITORY_URL,
)
from ....debugging import get_logger
from .util import get_command_output

_logger = get_logger("Repository Detection")


class DetectionError(Exception):
    pass


@attr.s
class Result(object):
    """" Repository information as queried by a detector """

    url = attr.ib(default="")
    branch = attr.ib(default="")
    commit = attr.ib(default="")
    root = attr.ib(default="")
    status = attr.ib(default="")
    diff = attr.ib(default="")
    modified = attr.ib(default=False, type=bool, converter=bool)

    def is_empty(self):
        return not any(attr.asdict(self).values())


@six.add_metaclass(abc.ABCMeta)
class Detector(object):
    """ Base class for repository detection """

    """ 
    Commands are represented using the result class, where each attribute contains 
    the command used to obtain the value of the same attribute in the actual result. 
    """

    _fallback = '_fallback'

    @attr.s
    class Commands(object):
        """" Repository information as queried by a detector """

        url = attr.ib(default=None, type=list)
        branch = attr.ib(default=None, type=list)
        commit = attr.ib(default=None, type=list)
        root = attr.ib(default=None, type=list)
        status = attr.ib(default=None, type=list)
        diff = attr.ib(default=None, type=list)
        modified = attr.ib(default=None, type=list)
        # alternative commands
        branch_fallback = attr.ib(default=None, type=list)

    def __init__(self, type_name, name=None):
        self.type_name = type_name
        self.name = name or type_name

    def _get_commands(self):
        """ Returns a RepoInfo instance containing a command for each info attribute """
        return self.Commands()

    def _get_command_output(self, path, name, command):
        """ Run a command and return its output """
        try:
            return get_command_output(command, path)

        except (CalledProcessError, UnicodeDecodeError) as ex:
            if not name.endswith(self._fallback):
                fallback_command = attr.asdict(self._get_commands()).get(name + self._fallback)
                if fallback_command:
                    try:
                        return get_command_output(fallback_command, path)
                    except (CalledProcessError, UnicodeDecodeError):
                        pass
            _logger.warning("Can't get {} information for {} repo in {}".format(name, self.type_name, path))
            # full details only in debug
            _logger.debug(
                "Can't get {} information for {} repo in {}: {}".format(
                    name, self.type_name, path, str(ex)
                )
            )
            return ""

    def _get_info(self, path, include_diff=False):
        """
        Get repository information.
        :param path: Path to repository
        :param include_diff: Whether to include the diff command's output (if available)
        :return: RepoInfo instance
        """
        path = str(path)
        commands = self._get_commands()
        if not include_diff:
            commands.diff = None

        info = Result(
            **{
                name: self._get_command_output(path, name, command)
                for name, command in attr.asdict(commands).items()
                if command and not name.endswith(self._fallback)
            }
        )

        return info

    def _post_process_info(self, info):
        # check if there are uncommitted changes in the current repository
        return info

    def get_info(self, path, include_diff=False):
        """
        Get repository information.
        :param path: Path to repository
        :param include_diff: Whether to include the diff command's output (if available)
        :return: RepoInfo instance
        """
        info = self._get_info(path, include_diff)
        return self._post_process_info(info)

    def _is_repo_type(self, script_path):
        try:
            with open(os.devnull, "wb") as devnull:
                return (
                    call(
                        [self.type_name, "status"],
                        stderr=devnull,
                        stdout=devnull,
                        cwd=str(script_path),
                    )
                    == 0
                )
        except CalledProcessError:
            _logger.warning("Can't get {} status".format(self.type_name))
        except (OSError, EnvironmentError, IOError):
            # File not found or can't be executed
            pass
        return False

    def exists(self, script_path):
        """
        Test whether the given script resides in
        a repository type represented by this plugin.
        """
        return self._is_repo_type(script_path)


class HgDetector(Detector):
    def __init__(self):
        super(HgDetector, self).__init__("hg")

    def _get_commands(self):
        return self.Commands(
            url=["hg", "paths", "--verbose"],
            branch=["hg", "--debug", "id", "-b"],
            commit=["hg", "--debug", "id", "-i"],
            root=["hg", "root"],
            status=["hg", "status"],
            diff=["hg", "diff"],
            modified=["hg", "status", "-m"],
        )

    def _post_process_info(self, info):
        if info.url:
            info.url = info.url.split(" = ")[1]

        if info.commit:
            info.commit = info.commit.rstrip("+")

        return info


class GitDetector(Detector):
    def __init__(self):
        super(GitDetector, self).__init__("git")

    def _get_commands(self):
        return self.Commands(
            url=["git", "remote", "get-url", "origin"],
            branch=["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            commit=["git", "rev-parse", "HEAD"],
            root=["git", "rev-parse", "--show-toplevel"],
            status=["git", "status", "-s"],
            diff=["git", "diff"],
            modified=["git", "ls-files", "-m"],
            branch_fallback=["git", "rev-parse", "--abbrev-ref", "HEAD"],
        )

    def _post_process_info(self, info):
        if info.url and not info.url.endswith(".git"):
            info.url += ".git"

        if (info.branch or "").startswith("origin/"):
            info.branch = info.branch[len("origin/") :]

        return info


class EnvDetector(Detector):
    def __init__(self, type_name):
        super(EnvDetector, self).__init__(type_name, "{} environment".format(type_name))

    def _is_repo_type(self, script_path):
        return VCS_REPO_TYPE.get(default="").lower() == self.type_name and bool(
            VCS_REPOSITORY_URL.get()
        )

    @staticmethod
    def _normalize_root(root):
        """
        Get the absolute location of the parent folder (where .git resides)
        """
        root_parts = list(reversed(Path(root).parts))
        cwd_abs = list(reversed(Path.cwd().parts))
        count = len(cwd_abs)
        for i, p in enumerate(cwd_abs):
            if i >= len(root_parts):
                break
            if p == root_parts[i]:
                count -= 1
        cwd_abs.reverse()
        root_abs_path = Path().joinpath(*cwd_abs[:count])
        return str(root_abs_path)

    def _get_info(self, _, include_diff=False):
        repository_url = VCS_REPOSITORY_URL.get()

        if not repository_url:
            raise DetectionError("No VCS environment data")

        return Result(
            url=repository_url,
            branch=VCS_BRANCH.get(),
            commit=VCS_COMMIT_ID.get(),
            root=VCS_ROOT.get(converter=self._normalize_root),
            status=VCS_STATUS.get(),
            diff=VCS_DIFF.get(),
        )


class GitEnvDetector(EnvDetector):
    def __init__(self):
        super(GitEnvDetector, self).__init__("git")


class HgEnvDetector(EnvDetector):
    def __init__(self):
        super(HgEnvDetector, self).__init__("hg")
