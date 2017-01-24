import os
import hashlib
import itertools
import operator
import copy
import re
import logging
import sys
import argparse


class DuplicateResolver(object):
    # Abstract base class

    def __init__(self, reverse=False):
        self.reverse = reverse

    def resolve(self, flist):
        return flist, []


class SortBasedDuplicateResolver(DuplicateResolver):
    # Resolver based on sorting on some attribute pulled from each entry
    # by a rank_function.

    def __init__(self, rank_function, reverse=False):
        self.rank_function = rank_function
        super(SortBasedDuplicateResolver, self).__init__(reverse)

    def resolve(self, flist):
        if len(flist) > 1:
            q = sorted(flist, key=self.rank_function, reverse=self.reverse)
            c = cmp(self.rank_function(q[0]),
                    self.rank_function(q[1]))

            pivot = None
            rank = self.rank_function(q[0])
            for i in range(1, len(q)):
                if self.rank_function(q[i]) != rank:
                    # Found the point where the sorting is meaningful
                    pivot = i
                    break

            if pivot is not None:
                return q[:pivot], q[pivot:]

        return flist, []


class AttrBasedDuplicateResolver(SortBasedDuplicateResolver):
    # Non-abstract base class for resolvers using an attrgetter to sort.
    def __init__(self, attribute, reverse=False):
        super(AttrBasedDuplicateResolver, self).__init__(operator.attrgetter(attribute), reverse)


class PathLengthDuplicateResolver(SortBasedDuplicateResolver):
    # Resolve based on the shortest path length (by component count)
    # excluding the source path.
    def __init__(self, reverse=False):
        super(PathLengthDuplicateResolver, self).__init__(lambda x:
                                                          len(x.path.split(os.path.sep))
                                                          - len(x.source.path.split(os.path.sep)),
                                                          reverse)


class SourceOrderDuplicateResolver(AttrBasedDuplicateResolver):
    # Resolve based on the order of the sources specfied on the command line.
    def __init__(self, reverse=False):
        super(SourceOrderDuplicateResolver, self).__init__('source.order', reverse)


class ModificationDateDuplicateResolver(AttrBasedDuplicateResolver):
    # Resolve based on file modification date.
        def __init__(self, reverse=False):
            super(ModificationDateDuplicateResolver, self).__init__('stat.st_mtime', reverse)


class CreationDateDuplicateResolver(AttrBasedDuplicateResolver):
    # Resolve based on file creation date.
        def __init__(self, reverse=False):
            super(CreationDateDuplicateResolver, self).__init__('stat.st_ctime', reverse)


class CopyPatternDuplicateResolver(DuplicateResolver):
    # Resolve by removing files whose names match common "copy" patterns.
    copy_patterns = [re.compile('^Copy of'), re.compile('.* copy [0-9]+\.[a-zA-Z0-9]+$')]

    def resolve(self, flist):
        determiner = lambda entry: reduce(operator.or_,
                                          [re.match(pattern, entry.path) is not None
                                           for pattern in self.copy_patterns])

        return (filter(lambda q: not determiner(q), flist),
                filter(determiner, flist))


class InteractiveDuplicateResolver(DuplicateResolver):
    # Allow the user to interactively resolve duplicate files.
    def resolve(self, flist):
        for i in range(len(flist)):
            print '%2d\t%s\n' % (i, flist[i])

        d = int(raw_input('Enter file to retain: '))

        dupes = copy.copy(flist)
        dupes.pop(d)
        return [flist[d]], dupes


class DuplicateFileSink(object):
    # Abstract base class
    def sink(self, files):
        pass


class DeleteDuplicateFileSink(object):
    # Immediately delete duplicate files.
    def sink(self, files):
        logger = logging.getLogger(__name__)
        for entry in files:
            try:
                logger.debug('Deleting duplicate file %s', entry.path)
                os.unlink(entry.path)
            except Exception as e:
                logger.error('Unable to delete duplicate file %s: %s', entry.path, e)


class SequesterDuplicateFileSink(object):
    # Move duplicate files into a separate directory tree

    def __init__(self, sequester_path=None):
        self.sequester_path = sequester_path

    def sink(self, files):
        logger = logging.getLogger(__name__)
        for entry in files:
            try:
                logger.debug('Sequestering duplicate file %s', entry.path)
                # We don't use os.renames because it has the bizarre side effect
                # of pruning directories containing the original file, if empty.

                # os.path.join will not correctly join if a subsequent path component
                # is an absolute path; hence we split before joining.
                new_path = os.path.join(self.sequester_path, *entry.path.split(os.path.sep))

                if not os.path.exists(os.path.split(new_path)[0]):
                    os.makedirs(os.path.split(new_path)[0])
                os.rename(entry.path, new_path)
            except Exception as e:
                logger.error('Unable to sequester duplicate file %s: %s', entry.path, e)


class OutputOnlyDuplicateFileSink(object):
    # Only output the names of duplicate files.

    def __init__(self, output_file=sys.stdout):
        self.output_file = output_file

    def sink(self, files):
        for entry in files:
            self.output_file.write(entry.path + '\n')


class FileEntry(object):
    def __init__(self, fpath, fsource):
        self.path = fpath
        self.source = fsource
        self.stat = os.stat(fpath)
        self.digest = None

    def get_size(self):
        return self.stat.st_size

    def get_digest(self):
        if self.digest is None:
            self.run_digest()

        return self.digest

    def run_digest(self):
        with open(self.path, mode='rb') as f:
            d = hashlib.sha512()

            while True:
                buf = f.read(4096)
                if not buf:
                    break

                d.update(buf)

        self.digest = d.hexdigest()
        logging.getLogger(__name__).debug('Found digest %s for path %s.', self.digest, self.path)


class FileCatalog(object):
    def __init__(self, idfunc):
        self.store = {}
        self.idfunc = idfunc

    def add_entry(self, entry):
        self.store.setdefault(self.idfunc(entry), []).append(entry)

    def get_groups(self):
        return [self.store[key] for key in self.store.keys() if len(self.store[key]) > 1]


class Source(object):
    def __init__(self, dpath, order):
        self.path = os.path.abspath(dpath)
        self.order = order

    def walk(self, ctx):
        for cwd, subdirs, files in os.walk(self.path):
            for f in files:
                # os.walk returns filenames, not paths
                ctx.add_entry(FileEntry(os.path.join(cwd, f), self))


class DeduplicateOperation(object):
    def __init__(self, sources, resolvers, sink):
        self.sources = sources
        self.resolvers = resolvers
        self.sink = sink

    def run(self):
        size_catalog = FileCatalog(lambda entry: entry.get_size())
        logger = logging.getLogger(__name__)

        # Initial pass through the file tree. Identify candidate duplicate
        # groups by equality of file size in bytes.
        logger.info('Building file catalog...')
        for s in self.sources:
            logger.info('Walking source %d at %s', s.order, s.path)
            s.walk(size_catalog)

        # Second pass: use SHA digest to confirm duplicate entries.
        logger.info('Identifying duplicate file groups...')

        f = FileCatalog(lambda entry: entry.get_digest())

        for entry in itertools.chain(*size_catalog.get_groups()):
            f.add_entry(entry)

        # Run confirmed duplicate groups through our chain of resolvers.
        to_sink = []

        for g in f.get_groups():
            logger.info('Attempting to resolve group of %d duplicate files:\n%s',
                        len(g),
                        '\n'.join(map(operator.attrgetter('path'), g)))
            originals = g

            for r in self.resolvers:
                logger.debug('Applying resolver %s.', r)
                (originals, duplicates) = r.resolve(originals)
                logger.debug('Resolver found duplicates:\n%s\n and originals:\n%s',
                             '\n'.join(map(operator.attrgetter('path'), duplicates)),
                             '\n'.join(map(operator.attrgetter('path'), originals)))

                if len(originals) > 0:
                    # The resolver returned more than one original. Sink the
                    # duplicates and proceed to the next resolver.
                    to_sink.extend(duplicates)
                elif len(originals) == 1:
                    # Narrowed to a single original file. Stop running resolvers
                    # on this group.
                    break
                else:
                    # If the resolver identified all of the files as duplicates,
                    # reset and punt to the next resolver.
                    originals = duplicates

            if len(originals) > 1:
                logger.info('Marking files as originals (unable to resolve duplicates):\n%s',
                            '\n'.join(map(operator.attrgetter('path'), originals)))
            else:
                logger.info('Marking file as original:\n%s',
                            originals[0].path)

        # Appropriately discard all of the identified duplicate files.
        logger.info('Finished. %d duplicate files located.', len(to_sink))
        self.sink.sink(to_sink)
