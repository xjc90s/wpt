import abc
import argparse
import ast
import json
import logging
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from typing import (Any, Callable, Dict, IO, Iterable, List, Optional, Sequence, Set, Text, Tuple,
                    Type, TypeVar)

from urllib.parse import urlsplit, urljoin, parse_qs

try:
    from xml.etree import cElementTree as ElementTree
except ImportError:
    from xml.etree import ElementTree as ElementTree  # type: ignore

from . import fnmatch
from . import rules
from .. import localpaths
from ..ci.tc.github_checks_output import get_gh_checks_outputter, GitHubChecksOutputter
from ..gitignore.gitignore import PathFilter
from ..wpt import testfiles
from ..manifest.mputil import max_parallelism
from ..manifest.vcs import walk

from ..manifest.sourcefile import SourceFile, js_meta_re, python_meta_re, space_chars, get_any_variants

from ..metadata.yaml.load import load_data_to_dict
from ..metadata.meta.schema import META_YML_FILENAME, MetaFile
from ..metadata.webfeatures.schema import WEB_FEATURES_YML_FILENAME, WebFeaturesFile

# The Ignorelist is a two level dictionary. The top level is indexed by
# error names (e.g. 'TRAILING WHITESPACE'). Each of those then has a map of
# file patterns (e.g. 'foo/*') to a set of specific line numbers for the
# exception. The line numbers are optional; if missing the entire file
# ignores the error.
Ignorelist = Dict[str, Dict[str, Set[Optional[int]]]]

# Define an arbitrary typevar
T = TypeVar("T")


logger: Optional[logging.Logger] = None


def setup_logging(prefix: bool = False) -> None:
    global logger
    if logger is None:
        logger = logging.getLogger(os.path.basename(os.path.splitext(__file__)[0]))
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        # Only add a handler if the parent logger is missing a handler
        parent = logger.parent
        assert isinstance(parent, logging.Logger)
        if parent and len(parent.handlers) == 0:
            handler = logging.StreamHandler(sys.stdout)
            logger.addHandler(handler)
    if prefix:
        format = logging.BASIC_FORMAT
    else:
        format = "%(message)s"
    formatter = logging.Formatter(format)
    for handler in logger.handlers:
        handler.setFormatter(formatter)
    logger.setLevel(logging.DEBUG)


setup_logging()


ERROR_MSG = """You must fix all errors; for details on how to fix them, see
https://web-platform-tests.org/writing-tests/lint-tool.html

However, instead of fixing a particular error, it's sometimes
OK to add a line to the lint.ignore file in the root of the
web-platform-tests directory to make the lint tool ignore it.

For example, to make the lint tool ignore all '%s'
errors in the %s file,
you could add the following line to the lint.ignore file.

%s: %s"""


def all_filesystem_paths(repo_root: Text, subdir: Optional[Text] = None) -> Iterable[Text]:
    path_filter = PathFilter(repo_root.encode("utf8"),
                             extras=[b".git/"])
    if subdir:
        expanded_path = subdir.encode("utf8")
        subdir_str = expanded_path
    else:
        expanded_path = repo_root.encode("utf8")
    for dirpath, dirnames, filenames in path_filter(walk(expanded_path)):
        for filename, _ in filenames:
            path = os.path.join(dirpath, filename)
            if subdir:
                path = os.path.join(subdir_str, path)
            assert not os.path.isabs(path), path
            yield path.decode("utf8")


def _all_files_equal(paths: Iterable[Text]) -> bool:
    """
    Checks all the paths are files that are byte-for-byte identical

    :param paths: the list of paths to compare
    :returns: True if they are all identical
    """
    paths = list(paths)
    if len(paths) < 2:
        return True

    first = paths.pop()
    size = os.path.getsize(first)
    if any(os.path.getsize(path) != size for path in paths):
        return False

    # Chunk this to avoid eating up memory and file descriptors
    bufsize = 4096*4  # 16KB, a "reasonable" number of disk sectors
    groupsize = 8  # Hypothesised to be large enough in the common case that everything fits in one group
    with open(first, "rb") as first_f:
        for start in range(0, len(paths), groupsize):
            path_group = paths[start:start+groupsize]
            first_f.seek(0)
            try:
                files = [open(x, "rb") for x in path_group]
                for _ in range(0, size, bufsize):
                    a = first_f.read(bufsize)
                    for f in files:
                        b = f.read(bufsize)
                        if a != b:
                            return False
            finally:
                for f in files:
                    f.close()

    return True


def check_path_length(repo_root: Text, path: Text) -> List[rules.Error]:
    if len(path) + 1 > 150:
        return [rules.PathLength.error(path, (path, len(path) + 1))]
    return []


def check_file_type(repo_root: Text, path: Text) -> List[rules.Error]:
    if os.path.islink(path):
        return [rules.FileType.error(path, (path, "symlink"))]
    return []


def check_worker_collision(repo_root: Text, path: Text) -> List[rules.Error]:
    endings = [(".any.html", ".any.js"),
               (".any.worker.html", ".any.js"),
               (".worker.html", ".worker.js")]
    for path_ending, generated in endings:
        if path.endswith(path_ending):
            return [rules.WorkerCollision.error(path, (path_ending, generated))]
    return []


def check_gitignore_file(repo_root: Text, path: Text) -> List[rules.Error]:
    if not path.endswith(".gitignore"):
        return []

    path_parts = path.split(os.path.sep)
    if len(path_parts) == 1:
        return []

    if path_parts[-1] != ".gitignore":
        return []

    if (path_parts[0] in ["tools", "docs"] or
        path_parts[:2] == ["resources", "webidl2"]):
        return []

    return [rules.GitIgnoreFile.error(path)]


def check_mojom_js(repo_root: Text, path: Text) -> List[rules.Error]:
    if path.endswith(".mojom.js"):
        return [rules.MojomJSFile.error(path)]
    return []


def check_ahem_copy(repo_root: Text, path: Text) -> List[rules.Error]:
    lpath = path.lower()
    if "ahem" in lpath and lpath.endswith(".ttf"):
        return [rules.AhemCopy.error(path)]
    return []


def check_tentative_directories(repo_root: Text, path: Text) -> List[rules.Error]:
    path_parts = path.split(os.path.sep)
    for directory in path_parts[:-1]:
        if "tentative" in directory and directory != "tentative":
            return [rules.TentativeDirectoryName.error(path)]
    return []


def check_git_ignore(repo_root: Text, paths: List[Text]) -> List[rules.Error]:
    errors = []

    with tempfile.TemporaryFile('w+', newline='') as f:
        for path in paths:
            f.write('%s\n' % os.path.join(repo_root, path))
        f.seek(0)
        try:
            matches = subprocess.check_output(
                ["git", "check-ignore", "--verbose", "--no-index", "--stdin"], stdin=f)
            for match in matches.strip().split(b'\n'):
                match_filter, path_bytes = match.split()
                _, _, filter_string = match_filter.split(b':')
                # If the matching filter reported by check-ignore is a special-case exception,
                # that's fine. Otherwise, it requires a new special-case exception.
                if filter_string[0:1] != b'!':
                    path = path_bytes.decode("utf8")
                    errors.append(rules.IgnoredPath.error(path, (path,)))
        except subprocess.CalledProcessError:
            # Nonzero return code means that no match exists.
            pass
    return errors


drafts_csswg_re = re.compile(r"https?\:\/\/drafts\.csswg\.org\/([^/?#]+)")
w3c_tr_re = re.compile(r"https?\:\/\/www\.w3c?\.org\/TR\/([^/?#]+)")
w3c_dev_re = re.compile(r"https?\:\/\/dev\.w3c?\.org\/[^/?#]+\/([^/?#]+)")


def check_unique_testharness_basenames(repo_root: Text, paths: List[Text]) -> List[rules.Error]:
    """
    Checks that all testharness files have unique basename paths.

    The 'basename path' refers to the entire path excluding the extension. For
    example, 'foo/bar/baz.html' and 'foo/bar/baz.xhtml' have the same basename
    path, but 'foo/bar/baz.html' and 'foo/qux/baz.html' do not.

    Testharness files with identical basenames have caused issues in downstream
    infrastructure (see https://github.com/web-platform-tests/wpt/issues/7570),
    and may cause confusion in general.

    :param repo_root: the repository root
    :param paths: list of all paths
    :returns: a list of errors found in ``paths``
    """

    errors = []
    file_dict = defaultdict(list)
    for path in paths:
        source_file = SourceFile(repo_root, path, "/")
        if "testharness" not in source_file.possible_types:
            continue
        file_name, file_extension = os.path.splitext(path)
        file_dict[file_name].append(file_extension)
    for k, v in file_dict.items():
        if len(v) == 1:
            continue
        context = (', '.join(v),)
        for extension in v:
            errors.append(rules.DuplicateBasenamePath.error(k + extension, context))
    return errors


def check_unique_case_insensitive_paths(repo_root: Text, paths: List[Text]) -> List[rules.Error]:
    seen: Dict[Text, Text] = {}
    errors = []
    for path in paths:
        lower_path = path.lower()
        if lower_path in seen:
            context = (seen[lower_path],)
            errors.append(rules.DuplicatePathCaseInsensitive.error(path, context))
        else:
            seen[lower_path] = path
    return errors


def parse_ignorelist(f: IO[Text]) -> Tuple[Ignorelist, Set[Text]]:
    """
    Parse the ignorelist file given by `f`, and return the parsed structure.

    :returns: a tuple of an Ignorelist and a set of files that are completely
              skipped by the linter (i.e. have a '*' entry).
    """

    data: Ignorelist = defaultdict(lambda:defaultdict(set))
    skipped_files: Set[Text] = set()

    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [item.strip() for item in line.split(":")]

        if len(parts) == 2:
            error_types_s, file_match = parts
            line_number: Optional[int] = None
        else:
            error_types_s, file_match, line_number_s = parts
            line_number = int(line_number_s)

        error_types = {item.strip() for item in error_types_s.split(",")}
        file_match = os.path.normcase(file_match)

        if "*" in error_types:
            skipped_files.add(file_match)
        else:
            for error_type in error_types:
                data[error_type][file_match].add(line_number)

    return data, skipped_files


def filter_ignorelist_errors(data: Ignorelist, errors: Sequence[rules.Error]) -> List[rules.Error]:
    """
    Filter out those errors that are ignored in `data`.
    """

    if not errors:
        return []

    skipped = [False for item in range(len(errors))]

    for i, (error_type, msg, path, line) in enumerate(errors):
        normpath = os.path.normcase(path)
        # Allow skipping all lint errors except the IGNORED PATH lint,
        # which explains how to fix it correctly and shouldn't be skipped.
        if error_type in data and error_type != "IGNORED PATH":
            wl_files = data[error_type]
            for file_match, allowed_lines in wl_files.items():
                if None in allowed_lines or line in allowed_lines:
                    if fnmatch.fnmatchcase(normpath, file_match):
                        skipped[i] = True

    return [item for i, item in enumerate(errors) if not skipped[i]]


regexps = [item() for item in  # type: ignore
           [rules.TrailingWhitespaceRegexp,
            rules.TabsRegexp,
            rules.CRRegexp,
            rules.SetTimeoutRegexp,
            rules.W3CTestOrgRegexp,
            rules.WebPlatformTestRegexp,
            rules.Webidl2Regexp,
            rules.ConsoleRegexp,
            rules.GenerateTestsRegexp,
            rules.PrintRegexp,
            rules.LayoutTestsRegexp,
            rules.MissingDepsRegexp,
            rules.SpecialPowersRegexp,
            rules.AssertThrowsRegexp,
            rules.PromiseRejectsRegexp,
            rules.AssertPreconditionRegexp,
            rules.HTMLInvalidSyntaxRegexp,
            rules.TestDriverInternalRegexp]]


def check_regexp_line(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    errors: List[rules.Error] = []

    applicable_regexps = [regexp for regexp in regexps if regexp.applies(path)]

    for i, line in enumerate(f):
        for regexp in applicable_regexps:
            if regexp.search(line):
                errors.append((regexp.name, regexp.description, path, i+1))

    return errors


def check_parsed(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    source_file = SourceFile(repo_root, path, "/", contents=f.read())

    errors: List[rules.Error] = []

    if path.startswith("css/"):
        if (source_file.type != "support" and
            not source_file.name_is_reference and
            not source_file.name_is_tentative and
            not source_file.name_is_crashtest and
            not source_file.spec_links):
            return [rules.MissingLink.error(path)]

    if source_file.name_is_non_test:
        return []

    if source_file.markup_type is None:
        return []

    if source_file.root is None:
        return [rules.ParseFailed.error(path)]

    test_type = source_file.type

    if test_type == "manual" and not source_file.name_is_manual:
        errors.append(rules.ContentManual.error(path))

    if test_type == "visual" and not source_file.name_is_visual:
        errors.append(rules.ContentVisual.error(path))

    about_blank_parts = urlsplit("about:blank")
    for reftest_node in source_file.reftest_nodes:
        href = reftest_node.attrib.get("href", "").strip(space_chars)
        parts = urlsplit(href)

        if parts == about_blank_parts:
            continue

        if (parts.scheme or parts.netloc):
            errors.append(rules.AbsoluteUrlRef.error(path, (href,)))
            continue

        ref_url = urljoin(source_file.url, href)
        ref_parts = urlsplit(ref_url)

        if source_file.url == ref_url:
            errors.append(rules.SameFileRef.error(path))
            continue

        assert ref_parts.path != ""

        reference_file = os.path.join(repo_root, ref_parts.path[1:])
        reference_rel = reftest_node.attrib.get("rel", "")

        if not os.path.isfile(reference_file):
            errors.append(rules.NonexistentRef.error(path,
                                                     (reference_rel, href)))

    if source_file.reftest_nodes:
        if test_type not in ("print-reftest", "reftest"):
            errors.append(rules.ReferenceInOtherType.error(path, (test_type,)))

    if len(source_file.timeout_nodes) > 1:
        errors.append(rules.MultipleTimeout.error(path))

    for timeout_node in source_file.timeout_nodes:
        timeout_value = timeout_node.attrib.get("content", "").lower()
        if timeout_value != "long":
            errors.append(rules.InvalidTimeout.error(path, (timeout_value,)))

    if source_file.content_is_ref_node or source_file.content_is_testharness:
        for element in source_file.variant_nodes:
            if "content" not in element.attrib:
                errors.append(rules.VariantMissing.error(path))
            else:
                variant = element.attrib["content"]
                if is_variant_malformed(variant):
                    value = f"{path} `<meta name=variant>` 'content' attribute"
                    errors.append(rules.MalformedVariant.error(path, (value,)))

    required_elements: List[Text] = []

    testharnessreport_nodes: List[ElementTree.Element] = []
    if source_file.testharness_nodes:
        if test_type not in ("testharness", "manual"):
            errors.append(rules.TestharnessInOtherType.error(path, (test_type,)))
        if len(source_file.testharness_nodes) > 1:
            errors.append(rules.MultipleTestharness.error(path))

        testharnessreport_nodes = source_file.root.findall(".//{http://www.w3.org/1999/xhtml}script[@src='/resources/testharnessreport.js']")
        if not testharnessreport_nodes:
            errors.append(rules.MissingTestharnessReport.error(path))
        else:
            if len(testharnessreport_nodes) > 1:
                errors.append(rules.MultipleTestharnessReport.error(path))

        required_elements.extend(key for key, value in {"testharness": True,
                                                        "testharnessreport": len(testharnessreport_nodes) > 0,
                                                        "timeout": len(source_file.timeout_nodes) > 0}.items()
                                 if value)

    testdriver_vendor_nodes: List[ElementTree.Element] = []
    if source_file.testdriver_nodes:
        if test_type not in {"testharness", "reftest", "print-reftest", "crashtest", "support"}:
            errors.append(rules.TestdriverInUnsupportedType.error(path, (test_type,)))

        if len(source_file.testdriver_nodes) > 1:
            errors.append(rules.MultipleTestdriver.error(path))

        testdriver_vendor_nodes = source_file.root.findall(".//{http://www.w3.org/1999/xhtml}script[@src='/resources/testdriver-vendor.js']")
        if not testdriver_vendor_nodes:
            errors.append(rules.MissingTestdriverVendor.error(path))
        else:
            if len(testdriver_vendor_nodes) > 1:
                errors.append(rules.MultipleTestdriverVendor.error(path))

        required_elements.append("testdriver")
        if len(testdriver_vendor_nodes) > 0:
            required_elements.append("testdriver-vendor")

    if required_elements:
        seen_elements = defaultdict(bool)

        for elem in source_file.root.iter():
            if source_file.timeout_nodes and elem == source_file.timeout_nodes[0]:
                seen_elements["timeout"] = True
                if seen_elements["testharness"]:
                    errors.append(rules.LateTimeout.error(path))

            elif source_file.testharness_nodes and elem == source_file.testharness_nodes[0]:
                seen_elements["testharness"] = True

            elif testharnessreport_nodes and elem == testharnessreport_nodes[0]:
                seen_elements["testharnessreport"] = True
                if not seen_elements["testharness"]:
                    errors.append(rules.EarlyTestharnessReport.error(path))

            elif source_file.testdriver_nodes and elem == source_file.testdriver_nodes[0]:
                seen_elements["testdriver"] = True

            elif testdriver_vendor_nodes and elem == testdriver_vendor_nodes[0]:
                seen_elements["testdriver-vendor"] = True
                if not seen_elements["testdriver"]:
                    errors.append(rules.EarlyTestdriverVendor.error(path))

            if all(seen_elements[name] for name in required_elements):
                break

    for element in source_file.root.findall(".//{http://www.w3.org/1999/xhtml}script[@src]"):
        src = element.attrib["src"]

        def is_path_correct(script: Text, src: Text) -> bool:
            """
            If the `src` relevant to the `script`, check that the `src` is the
            correct path for `script`.
            :param script: the script name to check the `src` for.
            :param src: the included path.
            :return: if the `src` irrelevant to the `script`, or if the `src`
                     path is the correct path.
            """
            if script == src:
                # The src does not provide the full path.
                return False

            if "/%s" % script not in src:
                # The src is not relevant to the script.
                return True

            return ("%s" % src).startswith("/resources/%s" % script)

        def is_query_string_correct(script: Text, src: Text,
                allowed_query_string_params: Dict[str, List[str]]) -> bool:
            """
            Checks if the query string in a script tag's `src` is valid.

            Specifically, it verifies that the query string parameters and their
            values are among those allowed for the given script. It handles vendor
            prefixes (parameters or values containing a colon) by allowing them
            unconditionally.

            :param script: the name of the script (e.g., "testharness.js"). Used
                           to verify is the given `src` is related to the
                           script.
            :param src: the full `src` attribute value from the script tag.
            :param allowed_query_string_params: A dictionary where keys are
                                                allowed parameter names and
                                                values are lists of allowed
                                                values for each parameter.
            :return: if the query string is empty or contains only allowed
                     params.
            """
            if not ("%s" % src).startswith("/resources/%s?" % script):
                # The src is not related to the script.
                return True

            try:
                query_string = urlsplit(urljoin(source_file.url, src)).query
                query_string_params = parse_qs(query_string,
                                               keep_blank_values=True)
            except ValueError:
                # Parsing error means that the query string is incorrect.
                return False

            for param_name in query_string_params:
                if param_name not in allowed_query_string_params:
                    return False

                for param_value in query_string_params[param_name]:
                    if ':' in param_value:
                        # Allow for vendor-specific values in query parameters.
                        continue
                    if param_value not in allowed_query_string_params[
                            param_name]:
                        return False
            return True

        if (not is_path_correct("testharness.js", src) or
                not is_query_string_correct("testharness.js", src, {})):
            errors.append(rules.TestharnessPath.error(path))

        if (not is_path_correct("testharnessreport.js", src) or
                not is_query_string_correct("testharnessreport.js", src, {})):
            errors.append(rules.TestharnessReportPath.error(path))

        if not is_path_correct("testdriver.js", src):
            errors.append(rules.TestdriverPath.error(path))
        if not is_query_string_correct("testdriver.js", src,
                                       {'feature': ['bidi']}):
            errors.append(rules.TestdriverUnsupportedQueryParameter.error(path))

        if (not is_path_correct("testdriver-vendor.js", src) or
                not is_query_string_correct("testdriver-vendor.js", src, {})):
            errors.append(rules.TestdriverVendorPath.error(path))

        script_path = None
        try:
            script_path = urlsplit(urljoin(source_file.url, src)).path
        except ValueError:
            # This happens if the contents of src isn't something that looks like a URL to Python
            pass
        if (script_path == "/common/reftest-wait.js" and
            "reftest-wait" not in source_file.root.attrib.get("class", "").split()):
            errors.append(rules.MissingReftestWait.error(path))

    return errors


def is_variant_malformed(variant: str) -> bool:
    return (variant == "" or variant[0] not in ("?", "#") or
            len(variant) == 1 or (variant[0] == "?" and variant[1] == "#"))


class ASTCheck(metaclass=abc.ABCMeta):
    @abc.abstractproperty
    def rule(self) -> Type[rules.Rule]:
        pass

    @abc.abstractmethod
    def check(self, root: ast.AST) -> List[int]:
        pass

class OpenModeCheck(ASTCheck):
    rule = rules.OpenNoMode

    def check(self, root: ast.AST) -> List[int]:
        errors = []
        for node in ast.walk(root):
            if isinstance(node, ast.Call):
                if hasattr(node.func, "id") and node.func.id in ("open", "file"):
                    if (len(node.args) < 2 and
                        all(item.arg != "mode" for item in node.keywords)):
                        errors.append(node.lineno)
        return errors

ast_checkers = [item() for item in [OpenModeCheck]]

def check_python_ast(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    if not path.endswith(".py"):
        return []

    try:
        root = ast.parse(f.read())
    except SyntaxError as e:
        return [rules.ParseFailed.error(path, line_no=e.lineno)]

    errors = []
    for checker in ast_checkers:
        for lineno in checker.check(root):
            errors.append(checker.rule.error(path, line_no=lineno))
    return errors


broken_js_metadata = re.compile(br"//\s*META:")
broken_python_metadata = re.compile(br"#\s*META:")


def check_global_metadata(value: bytes) -> Iterable[Tuple[Type[rules.Rule], Tuple[Any, ...]]]:
    global_values = {item.strip().decode("utf8") for item in value.split(b",") if item.strip()}

    # TODO: this could check for duplicates and such
    for global_value in global_values:
        if not get_any_variants(global_value):
            yield (rules.UnknownGlobalMetadata, ())


def check_script_metadata(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    if path.endswith((".window.js", ".worker.js", ".any.js")):
        meta_re = js_meta_re
        broken_metadata = broken_js_metadata
    elif path.endswith(".py"):
        meta_re = python_meta_re
        broken_metadata = broken_python_metadata
    else:
        return []

    done = False
    errors = []
    for line_no, line in enumerate(f, 1):
        assert isinstance(line, bytes), line

        m = meta_re.match(line)
        if m:
            key, value = m.groups()
            if key == b"global":
                for rule_class, context in check_global_metadata(value):
                    errors.append(rule_class.error(path, context, line_no))
            elif key == b"timeout":
                if value != b"long":
                    errors.append(rules.UnknownTimeoutMetadata.error(path,
                                                                     line_no=line_no))
            elif key == b"variant":
                if is_variant_malformed(value.decode()):
                    value = f"{path} `META: variant=...` value"
                    errors.append(rules.MalformedVariant.error(path, (value,), line_no))
            elif key == b"script":
                if value == b"/resources/testharness.js":
                    errors.append(rules.MultipleTestharness.error(path, line_no=line_no))
                elif value == b"/resources/testharnessreport.js":
                    errors.append(rules.MultipleTestharnessReport.error(path, line_no=line_no))
            elif key not in (b"title", b"quic"):
                errors.append(rules.UnknownMetadata.error(path, line_no=line_no))
        else:
            done = True

        if done:
            if meta_re.match(line):
                errors.append(rules.StrayMetadata.error(path, line_no=line_no))
            elif meta_re.search(line):
                errors.append(rules.IndentedMetadata.error(path, line_no=line_no))
            elif broken_metadata.search(line):
                errors.append(rules.BrokenMetadata.error(path, line_no=line_no))

    return errors


ahem_font_re = re.compile(br"font.*:.*ahem", flags=re.IGNORECASE)
# Ahem can appear either in the global location or in the support
# directory for legacy Mozilla imports
ahem_stylesheet_re = re.compile(br"\/fonts\/ahem\.css|support\/ahem.css",
                                flags=re.IGNORECASE)


def check_ahem_system_font(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    if not path.endswith((".html", ".htm", ".xht", ".xhtml")):
        return []
    contents = f.read()
    errors = []
    if ahem_font_re.search(contents) and not ahem_stylesheet_re.search(contents):
        errors.append(rules.AhemSystemFont.error(path))
    return errors


def check_meta_file(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    if os.path.basename(path) != META_YML_FILENAME:
        return []
    try:
        MetaFile(load_data_to_dict(f))
    except Exception:
        return [rules.InvalidMetaFile.error(path)]
    return []


def check_web_features_file(repo_root: Text, path: Text, f: IO[bytes]) -> List[rules.Error]:
    if os.path.basename(path) != WEB_FEATURES_YML_FILENAME:
        return []
    try:
        web_features_file: WebFeaturesFile = WebFeaturesFile(load_data_to_dict(f))
    except Exception:
        return [rules.InvalidWebFeaturesFile.error(path)]
    errors = []
    base_dir = os.path.join(repo_root, os.path.dirname(path))
    files_in_directory = [
        f for f in os.listdir(base_dir) if os.path.isfile(os.path.join(base_dir, f))]
    for feature in web_features_file.features:
        if isinstance(feature.files, list):
            for file in feature.files:
                if not file.match_files(files_in_directory):
                    errors.append(rules.MissingTestInWebFeaturesFile.error(path, (file)))

    return errors


def check_path(repo_root: Text, path: Text) -> List[rules.Error]:
    """
    Runs lints that check the file path.

    :param repo_root: the repository root
    :param path: the path of the file within the repository
    :returns: a list of errors found in ``path``
    """

    errors = []
    for path_fn in path_lints:
        errors.extend(path_fn(repo_root, path))
    return errors


def check_all_paths(repo_root: Text, paths: List[Text]) -> List[rules.Error]:
    """
    Runs lints that check all paths globally.

    :param repo_root: the repository root
    :param paths: a list of all the paths within the repository
    :returns: a list of errors found in ``f``
    """

    errors = []
    for paths_fn in all_paths_lints():
        errors.extend(paths_fn(repo_root, paths))
    return errors


def check_file_contents(repo_root: Text, path: Text, f: Optional[IO[bytes]] = None) -> List[rules.Error]:
    """
    Runs lints that check the file contents.

    :param repo_root: the repository root
    :param path: the path of the file within the repository
    :param f: a file-like object with the file contents
    :returns: a list of errors found in ``f``
    """
    if f is None:
        f = open(os.path.join(repo_root, path), 'rb')
    with f:
        errors = []
        for file_fn in file_lints:
            errors.extend(file_fn(repo_root, path, f))
            f.seek(0)
        return errors


def check_file_contents_apply(args: Tuple[Text, Text]) -> List[rules.Error]:
    return check_file_contents(*args)


def output_errors_text(log: Callable[[Any], None], errors: List[rules.Error]) -> None:
    for error_type, description, path, line_number in errors:
        pos_string = path
        if line_number:
            pos_string += ":%s" % line_number
        log(f"{pos_string}: {description} ({error_type})")


def output_errors_markdown(log: Callable[[Any], None], errors: List[rules.Error]) -> None:
    if not errors:
        return
    heading = """Got lint errors:

| Error Type | Position | Message |
|------------|----------|---------|"""
    for line in heading.split("\n"):
        log(line)
    for error_type, description, path, line_number in errors:
        pos_string = path
        if line_number:
            pos_string += ":%s" % line_number
        log(f"{error_type} | {pos_string} | {description} |")


def output_errors_json(log: Callable[[Any], None], errors: List[rules.Error]) -> None:
    for error_type, error, path, line_number in errors:
        # We use 'print' rather than the log function to ensure that the output
        # is valid JSON (e.g. with no logger preamble).
        print(json.dumps({"path": path, "lineno": line_number,
                          "rule": error_type, "message": error}))


def output_errors_github_checks(outputter: GitHubChecksOutputter, errors: List[rules.Error], first_reported: bool) -> None:
    """Output errors to the GitHub Checks output markdown format.

    :param outputter: the GitHub Checks outputter
    :param errors: a list of error tuples (error type, message, path, line number)
    :param first_reported: True if these are the first reported errors
    """
    if first_reported:
        outputter.output(
            "\nChanges in this PR contain lint errors, listed below. These "
            "errors must either be fixed or added to the list of ignored "
            "errors; see [the documentation]("
            "https://web-platform-tests.org/writing-tests/lint-tool.html). "
            "For help, please tag `@web-platform-tests/wpt-core-team` in a "
            "comment.\n")
        outputter.output("```")
    output_errors_text(outputter.output, errors)


def output_error_count(error_count: Dict[Text, int]) -> None:
    if not error_count:
        return

    assert logger is not None
    by_type = " ".join("%s: %d" % item for item in error_count.items())
    count = sum(error_count.values())
    logger.info("")
    if count == 1:
        logger.info(f"There was 1 error ({by_type})")
    else:
        logger.info("There were %d errors (%s)" % (count, by_type))


def changed_files(wpt_root: Text) -> List[Text]:
    revish = testfiles.get_revish(revish=None)
    changed, _ = testfiles.files_changed(revish, None, include_uncommitted=True, include_new=True)
    return [os.path.relpath(item, wpt_root) for item in changed]


def lint_paths(kwargs: Dict[Text, Any], wpt_root: Text) -> List[Text]:
    if kwargs.get("paths"):
        paths = []
        for path in kwargs.get("paths", []):
            if os.path.isdir(path):
                path_dir = list(all_filesystem_paths(wpt_root, path))
                paths.extend(path_dir)
            elif os.path.isfile(path):
                paths.append(os.path.relpath(os.path.abspath(path), wpt_root))
    elif kwargs["all"]:
        paths = list(all_filesystem_paths(wpt_root))
    elif kwargs["paths_file"]:
        paths = []
        with open(kwargs["paths_file"], 'r', newline='') as f:
            for line in f.readlines():
                path = line.strip()
                if os.path.isdir(path):
                    path_dir = list(all_filesystem_paths(wpt_root, path))
                    paths.extend(path_dir)
                elif os.path.isfile(path):
                    paths.append(os.path.relpath(os.path.abspath(path), wpt_root))
    else:
        changed_paths = changed_files(wpt_root)
        force_all = False
        for path in changed_paths:
            path = path.replace(os.path.sep, "/")
            if path == "lint.ignore" or path.startswith("tools/lint/"):
                force_all = True
                break
        paths = (list(changed_paths) if not force_all
                 else list(all_filesystem_paths(wpt_root)))

    return paths


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*",
                        help="List of paths to lint")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON format")
    parser.add_argument("--markdown", action="store_true",
                        help="Output markdown")
    parser.add_argument("--repo-root", type=str,
                        help="The WPT directory. Use this "
                        "option if the lint script exists outside the repository")
    parser.add_argument("--ignore-glob", type=str, action="append",
                        help="Additional file glob to ignore (repeat to add more). "
                        "Globs are matched against paths relative to REPO_ROOT "
                        "using fnmatch, except that path separators are normalized.")
    parser.add_argument("--all", action="store_true", help="If no paths are passed, try to lint the whole "
                        "working directory, not just files that changed")
    parser.add_argument("--github-checks-text-file", type=str,
                        help="Path to GitHub checks output file for Taskcluster runs")
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help="Level to parallelism to use (defaults to 0, which detects the number of CPUs)")
    parser.add_argument("--paths-file", help="File containing a list of files to lint, one per line")
    return parser


def main(venv: Any = None, **kwargs: Any) -> int:

    assert logger is not None
    if kwargs.get("json") and kwargs.get("markdown"):
        logger.critical("Cannot specify --json and --markdown")
        sys.exit(2)

    repo_root = kwargs.get('repo_root') or localpaths.repo_root
    output_format = {(True, False): "json",
                     (False, True): "markdown",
                     (False, False): "normal"}[(kwargs.get("json", False),
                                                kwargs.get("markdown", False))]

    if output_format == "markdown":
        setup_logging(True)

    paths = lint_paths(kwargs, repo_root)

    ignore_glob = kwargs.get("ignore_glob", [])

    github_checks_outputter = get_gh_checks_outputter(kwargs["github_checks_text_file"])

    jobs = kwargs.get("jobs", 0)

    return lint(repo_root, paths, output_format, ignore_glob, github_checks_outputter, jobs)


# best experimental guess at a decent cut-off for using the parallel path
MIN_FILES_FOR_PARALLEL = 80


def lint(repo_root: Text,
         paths: List[Text],
         output_format: Text,
         ignore_glob: Optional[List[Text]] = None,
         github_checks_outputter: Optional[GitHubChecksOutputter] = None,
         jobs: int = 0) -> int:
    error_count: Dict[Text, int] = defaultdict(int)
    last = None

    if jobs == 0:
        jobs = max_parallelism()

    with open(os.path.join(repo_root, "lint.ignore")) as f:
        ignorelist, skipped_files = parse_ignorelist(f)

    if ignore_glob:
        skipped_files |= set(ignore_glob)

    output_errors = {"json": output_errors_json,
                     "markdown": output_errors_markdown,
                     "normal": output_errors_text}[output_format]

    def process_errors(errors: List[rules.Error]) -> Optional[Tuple[Text, Text]]:
        """
        Filters and prints the errors, and updates the ``error_count`` object.

        :param errors: a list of error tuples (error type, message, path, line number)
        :returns: ``None`` if there were no errors, or
                  a tuple of the error type and the path otherwise
        """

        errors = filter_ignorelist_errors(ignorelist, errors)
        if not errors:
            return None

        assert logger is not None
        output_errors(logger.error, errors)

        if github_checks_outputter:
            first_output = len(error_count) == 0
            output_errors_github_checks(github_checks_outputter, errors, first_output)

        for error_type, error, path, line in errors:
            error_count[error_type] += 1

        return (errors[-1][0], path)

    to_check_content = []
    skip = set()

    for path in paths:
        abs_path = os.path.join(repo_root, path)
        if not os.path.exists(abs_path):
            skip.add(path)
            continue

        if any(fnmatch.fnmatch(path, file_match) for file_match in skipped_files):
            skip.add(path)
            continue

        errors = check_path(repo_root, path)
        last = process_errors(errors) or last

        if not os.path.isdir(abs_path):
            to_check_content.append((repo_root, path))

    paths = [p for p in paths if p not in skip]

    if jobs > 1 and len(to_check_content) >= MIN_FILES_FOR_PARALLEL:
        pool = multiprocessing.Pool(jobs)
        # submit this job first, as it's the longest running
        all_paths_result = pool.apply_async(check_all_paths, (repo_root, paths))
        # each item tends to be quick, so pass things in large chunks to avoid too much IPC overhead
        errors_it = pool.imap_unordered(check_file_contents_apply, to_check_content, chunksize=40)
        pool.close()
        for errors in errors_it:
            last = process_errors(errors) or last

        errors = all_paths_result.get()
        pool.join()
        last = process_errors(errors) or last
    else:
        for item in to_check_content:
            errors = check_file_contents(*item)
            last = process_errors(errors) or last

        errors = check_all_paths(repo_root, paths)
        last = process_errors(errors) or last

    if output_format in ("normal", "markdown"):
        output_error_count(error_count)
        if error_count:
            assert last is not None
            assert logger is not None
            for line in (ERROR_MSG % (last[0], last[1], last[0], last[1])).split("\n"):
                logger.info(line)

    if error_count and github_checks_outputter:
        github_checks_outputter.output("```")

    return sum(error_count.values())


path_lints = [check_file_type, check_path_length, check_worker_collision, check_ahem_copy,
              check_mojom_js, check_tentative_directories, check_gitignore_file]
file_lints = [check_regexp_line, check_parsed, check_python_ast, check_script_metadata,
              check_ahem_system_font, check_meta_file, check_web_features_file]


def all_paths_lints() -> Any:
    paths = [check_unique_testharness_basenames,
             check_unique_case_insensitive_paths]
    # Don't break users of the lint that don't have git installed.
    try:
        subprocess.check_output(["git", "--version"])
        paths += [check_git_ignore]
    except (subprocess.CalledProcessError, FileNotFoundError):
        print('No git present; skipping .gitignore lint.')
    return paths


if __name__ == "__main__":
    args = create_parser().parse_args()
    error_count = main(**vars(args))
    if error_count > 0:
        sys.exit(1)
