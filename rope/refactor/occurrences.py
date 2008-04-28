import re

import rope.base.pynames
from rope.base import (pynames, pyobjects, codeanalyze,
                       evaluate, exceptions, utils)


class Finder(object):
    """For finding occurrences of a name

    The constructor takes a `filters` argument.  It should be a list
    of functions that take a single argument.  For each possible
    occurrence, these functions are called in order with the an
    instance of `Occurrence`:

      * If it returns `None` other filters are tried.
      * If it returns `True`, the occurrence will be a match.
      * If it returns `False`, the occurrence will be skipped.
      * If all of the filters return `None`, it is skipped also.

    """

    def __init__(self, pycore, name, filters=[lambda o: True], docs=False):
        self.pycore = pycore
        self.name = name
        self.filters = filters
        self._textual_finder = _TextualFinder(name, docs=docs)

    def find_occurrences(self, resource=None, pymodule=None):
        """Generate `Occurrence` instances"""
        tools = _OccurrenceToolsCreator(self.pycore, resource=resource,
                                        pymodule=pymodule)
        for offset in self._textual_finder.find_offsets(tools.source_code):
            occurrence = Occurrence(tools, offset)
            for filter in self.filters:
                result = filter(occurrence)
                if result is None:
                    continue
                if result:
                    yield occurrence
                break


def create_finder(pycore, name, pyname, only_calls=False, imports=True,
                  unsure=None, docs=False, instance=None, in_hierarchy=False):
    """A factory for `Finder`

    Based on the arguments it creates a list of filters.  `instance`
    argument is needed only when you want implicit interfaces to be
    considered.

    """
    pynames = set([pyname])
    filters = []
    if only_calls:
        filters.append(CallsFilter())
    if not imports:
        filters.append(NoImportsFilter())
    if isinstance(instance, rope.base.pynames.ParameterName):
        for pyobject in instance.get_objects():
            try:
                pynames.add(pyobject[name])
            except exceptions.AttributeNotFoundError:
                pass
    for pyname in pynames:
        filters.append(PyNameFilter(pyname))
        if in_hierarchy:
            filters.append(InHierarchyFilter(pyname))
    if unsure:
        filters.append(UnsureFilter(unsure))
    return Finder(pycore, name, filters=filters, docs=docs)


class Occurrence(object):

    def __init__(self, tools, offset):
        self.tools = tools
        self.offset = offset
        self.resource = tools.resource

    @utils.cacheit
    def get_word_range(self):
        return self.tools.word_finder.get_word_range(self.offset)

    @utils.cacheit
    def get_primary_range(self):
        return self.tools.word_finder.get_primary_range(self.offset)

    @utils.cacheit
    def get_pyname(self):
        try:
            return self.tools.name_finder.get_pyname_at(self.offset)
        except exceptions.BadIdentifierError:
            pass

    @utils.cacheit
    def get_primary_and_pyname(self):
        try:
            return self.tools.name_finder.get_primary_and_pyname_at(self.offset)
        except exceptions.BadIdentifierError:
            pass

    @utils.cacheit
    def is_in_import_statement(self):
        return (self.tools.word_finder.is_from_statement(self.offset) or
                self.tools.word_finder.is_import_statement(self.offset))

    def is_called(self):
        return self.tools.word_finder.is_a_function_being_called(self.offset)

    def is_defined(self):
        return self.tools.word_finder.is_a_class_or_function_name_in_header(self.offset)

    def is_a_fixed_primary(self):
        return self.tools.word_finder.is_a_class_or_function_name_in_header(self.offset) or \
               self.tools.word_finder.is_a_name_after_from_import(self.offset)

    def is_written(self):
        return self.tools.word_finder.is_assigned_here(self.offset)

    def is_unsure(self):
        return unsure_pyname(self.get_pyname())


def same_pyname(expected, pyname):
    """Check whether `expected` and `pyname` are the same"""
    if expected is None or pyname is None:
        return False
    if expected == pyname:
        return True
    if type(expected) not in (pynames.ImportedModule, pynames.ImportedName) and \
       type(pyname) not in (pynames.ImportedModule, pynames.ImportedName):
        return False
    return expected.get_definition_location() == pyname.get_definition_location() and \
           expected.get_object() == pyname.get_object()

def unsure_pyname(pyname, unbound=True):
    """Return `True` if we don't know what this name references"""
    if pyname is None:
        return True
    if unbound and not isinstance(pyname, pynames.UnboundName):
        return False
    if pyname.get_object() == pyobjects.get_unknown():
        return True


class PyNameFilter(object):
    """For finding occurrences of a name"""

    def __init__(self, pyname):
        self.pyname = pyname

    def __call__(self, occurrence):
        if same_pyname(self.pyname, occurrence.get_pyname()):
            return True


class InHierarchyFilter(object):
    """For finding occurrences of a name"""

    def __init__(self, pyname, implementations_only=False):
        self.pyname = pyname
        self.impl_only = implementations_only
        self.pyclass = self._get_containing_class(pyname)
        if self.pyclass is not None:
            self.name = pyname.get_object().get_name()
            self.roots = self._get_root_classes(self.pyclass, self.name)
        else:
            self.roots = None

    def __call__(self, occurrence):
        if self.roots is None:
            return
        pyclass = self._get_containing_class(occurrence.get_pyname())
        if pyclass is not None:
            roots = self._get_root_classes(pyclass, self.name)
            if self.roots.intersection(roots):
                return True

    def _get_containing_class(self, pyname):
        if isinstance(pyname, pynames.DefinedName):
            scope = pyname.get_object().get_scope()
            parent = scope.parent
            if parent is not None and parent.get_kind() == 'Class':
                return parent.pyobject

    def _get_root_classes(self, pyclass, name):
        if self.impl_only and pyclass == self.pyclass:
            return set([pyclass])
        result = set()
        for superclass in pyclass.get_superclasses():
            if name in superclass:
                result.update(self._get_root_classes(superclass, name))
        if not result:
            return set([pyclass])
        return result


class UnsureFilter(object):

    def __init__(self, unsure):
        self.unsure = unsure

    def __call__(self, occurrence):
        if occurrence.is_unsure() and self.unsure(occurrence):
            return True


class NoImportsFilter(object):

    def __call__(self, occurrence):
        if occurrence.is_in_import_statement():
            return False


class CallsFilter(object):

    def __call__(self, occurrence):
        if not occurrence.is_called():
            return False


class _TextualFinder(object):

    def __init__(self, name, docs=False):
        self.name = name
        self.docs = docs
        self.comment_pattern = _TextualFinder.any('comment', [r'#[^\n]*'])
        self.string_pattern = _TextualFinder.any(
            'string', [codeanalyze.get_string_pattern()])
        self.pattern = self._get_occurrence_pattern(self.name)

    def find_offsets(self, source):
        if not self._fast_file_query(source):
            return
        if self.docs:
            searcher = self._normal_search
        else:
            searcher = self._re_search
        for matched in searcher(source):
            yield matched

    def _re_search(self, source):
        for match in self.pattern.finditer(source):
            for key, value in match.groupdict().items():
                if value and key == 'occurrence':
                    yield match.start(key)

    def _normal_search(self, source):
        current = 0
        while True:
            try:
                found = source.index(self.name, current)
                current = found + len(self.name)
                if (found == 0 or not self._is_id_char(source[found - 1])) and \
                   (current == len(source) or not self._is_id_char(source[current])):
                    yield found
            except ValueError:
                break

    def _is_id_char(self, c):
        return c.isalnum() or c == '_'

    def _fast_file_query(self, source):
        try:
            source.index(self.name)
            return True
        except ValueError:
            return False

    def _get_source(self, resource, pymodule):
        if resource is not None:
            return resource.read()
        else:
            return pymodule.source_code

    def _get_occurrence_pattern(self, name):
        occurrence_pattern = _TextualFinder.any('occurrence',
                                                 ['\\b' + name + '\\b'])
        pattern = re.compile(occurrence_pattern + '|' + self.comment_pattern +
                             '|' + self.string_pattern)
        return pattern

    @staticmethod
    def any(name, list_):
        return '(?P<%s>' % name + '|'.join(list_) + ')'


class _OccurrenceToolsCreator(object):

    def __init__(self, pycore, resource=None, pymodule=None):
        self.pycore = pycore
        self.resource = resource
        self.pymodule = pymodule

    @property
    @utils.cacheit
    def name_finder(self):
        if self.pymodule is None:
            self.pymodule = self.pycore.resource_to_pyobject(self.resource)
        return evaluate.ScopeNameFinder(self.pymodule)

    @property
    @utils.cacheit
    def source_code(self):
        if self.resource is not None:
            return self.resource.read()
        else:
            return self.pymodule.source_code

    @property
    @utils.cacheit
    def word_finder(self):
        return codeanalyze.WordRangeFinder(self.source_code)
