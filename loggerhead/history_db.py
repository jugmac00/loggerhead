# Copyright (C) 2010 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Store history information in a database."""

try:
    from sqlite3 import dbapi2
except ImportError:
    from pysqlite2 import dbapi2

from collections import defaultdict, deque
import time

from bzrlib import (
    debug,
    errors,
    lru_cache,
    revision,
    static_tuple,
    trace,
    ui,
    )

from loggerhead import history_db_schema as schema


NULL_PARENTS = (revision.NULL_REVISION,)


def _n_params(n):
    """Create a query string representing N parameters.

    n=1 => ?
    n=2 => ?, ?
    etc.
    """
    return ', '.join('?'*n)


def _add_n_params(query, n):
    """Add n parameters to the query string.

    the query should have a single '%s' in it to be expanded.
    """
    return query % (_n_params(n),)


def _get_result_for_many(cursor, query, params):
    """Get the results for a query with a lot of parameters.

    SQLite has a limit on how many parameters can be passed (often for a good
    reason). However, we don't want to have to think about it right now. So
    this just loops over params based on a maximum allowed per query. Then
    return the whole result in one list.

    :param query: An SQL query that should contain something like:
        "WHERE foo IN (%s)"
    :param params: A list of parameters to supply to the function.
    """
    res = []
    MAX_COUNT = 200
    for start in xrange(0, len(params), MAX_COUNT):
        next_params = params[start:start+MAX_COUNT]
        res.extend(
            cursor.execute(_add_n_params(query, len(next_params)),
                           next_params).fetchall())
    return res


class Importer(object):
    """Import data from bzr into the history_db."""

    _MAINLINE_PARENT_RANGE_LEN = 100

    def __init__(self, db_path, a_branch, tip_revision_id=None,
                 incremental=False, validate=False):
        db_conn = dbapi2.connect(db_path)
        self._incremental = incremental
        self._validate = validate
        self._db_conn = db_conn
        self._ensure_schema()
        self._cursor = self._db_conn.cursor()
        self._branch = a_branch
        if tip_revision_id is None:
            self._branch_tip_rev_id = a_branch.last_revision()
        else:
            self._branch_tip_rev_id = tip_revision_id
        self._branch_tip_key = (self._branch_tip_rev_id,)
        self._graph = None
        if not self._incremental:
            self._ensure_graph()
        self._rev_id_to_db_id = {}
        self._db_id_to_rev_id = {}
        self._stats = defaultdict(lambda: 0)
        # Map child_id => [parent_db_ids]
        self._db_parent_map = {}

    def set_max_cache_size(self, size):
        """Tell SQLite how many megabytes to cache internally."""
        page_size = self._db_conn.execute('PRAGMA page_size').fetchone()[0]
        self._db_conn.execute("PRAGMA cache_size = %d"
                              % (size / page_size,));

    def _ensure_schema(self):
        if not schema.is_initialized(self._db_conn, dbapi2.OperationalError):
            schema.create_sqlite_db(self._db_conn)
            if 'history_db' in debug.debug_flags:
                trace.note('history_db initialized database')
            # We know we can't do this incrementally, because nothing has
            # existed before...
            #self._incremental = False

    def _ensure_revisions(self, revision_ids):
        schema.ensure_revisions(self._cursor, revision_ids,
                                self._rev_id_to_db_id,
                                self._db_id_to_rev_id, self._graph)

    def _ensure_graph(self):
        if self._graph is not None:
            return
        repo = self._branch.repository
        self._graph = repo.revisions.get_known_graph_ancestry(
            [self._branch_tip_key])

    def _is_imported(self, tip_rev_id):
        res = self._cursor.execute(
            "SELECT tip_revision FROM dotted_revno JOIN revision"
            "    ON dotted_revno.tip_revision = revision.db_id"
            " WHERE revision_id = ?"
            "   AND tip_revision = merged_revision",
            (tip_rev_id,)).fetchone()
        return (res is not None)

    def _insert_nodes(self, tip_rev_id, nodes):
        """Insert all of the nodes mentioned into the database."""
        self._stats['_insert_node_calls'] += 1
        rev_to_db = self._rev_id_to_db_id
        tip_db_id = rev_to_db[tip_rev_id]
        self._stats['total_nodes_inserted'] += len(nodes)
        revno_entries = []
        st = static_tuple.StaticTuple
        def build_revno_info():
            for dist, node in enumerate(nodes):
                # TODO: Do we need to track the 'end_of_merge' and 'merge_depth'
                #       fields?
                db_id = rev_to_db[node.key[0]]
                revno_entries.append((tip_db_id,
                                      db_id,
                                      '.'.join(map(str, node.revno)),
                                      node.end_of_merge,
                                      node.merge_depth,
                                      dist))
        build_revno_info()
        try:
            schema.create_dotted_revnos(self._cursor, revno_entries)
        except dbapi2.IntegrityError:
            # Revisions already exist
            return False
        return True

    def _update_parents(self, nodes):
        """Update parent information for all these nodes."""
        # Get the keys and their parents
        parent_keys = self._graph.get_parent_keys
        parent_map = dict([(n.key[0], [p[0] for p in parent_keys(n.key)])
                           for n in nodes])
        self._insert_parent_map(parent_map)

    def _insert_parent_map(self, parent_map):
        """Insert all the entries in this parent map into the parent table."""
        r_to_d = self._rev_id_to_db_id
        def _ensure_parent_map_keys():
            rev_ids = set([r for r in parent_map if r not in r_to_d])
            rev_ids_update = rev_ids.update
            for parent_keys in parent_map.itervalues():
                rev_ids_update([p for p in parent_keys if p not in r_to_d])
            self._ensure_revisions(rev_ids)
        _ensure_parent_map_keys()
        data = []
        stuple = static_tuple.StaticTuple.from_sequence
        for rev_id, parent_ids in parent_map.iteritems():
            db_id = r_to_d[rev_id]
            if db_id in self._db_parent_map:
                # This has already been imported, skip it
                continue
            parent_db_ids = stuple([r_to_d[p_id] for p_id in parent_ids])
            self._db_parent_map[db_id] = parent_db_ids
            for idx, parent_db_id in enumerate(parent_db_ids):
                data.append((db_id, parent_db_id, idx))
        # Inserting the data in db-sorted order actually improves perf a fair
        # amount. ~10%. My guess is that it keeps locality for uniqueness
        # checks, etc.
        data.sort()
        self._cursor.executemany("INSERT OR IGNORE INTO parent"
                                 "  (child, parent, parent_idx)"
                                 "VALUES (?, ?, ?)", data)

    def do_import(self):
        if revision.is_null(self._branch_tip_rev_id):
            return
        merge_sorted = self._import_tip(self._branch_tip_rev_id)
        self._db_conn.commit()

    def _get_merge_sorted_tip(self, tip_revision_id):
        if self._incremental:
            self._update_ancestry(tip_revision_id)
            self._ensure_revisions([tip_revision_id])
            tip_db_id = self._rev_id_to_db_id[tip_revision_id]
            inc_merger = _IncrementalMergeSort(self, tip_db_id)
            merge_sorted = inc_merger.topo_order()
            # Map db_ids back to the keys that self._graph would generate
            # Assert that the result is valid
            if self._validate:
                self._ensure_graph()
                actual_ms = self._graph.merge_sort((tip_revision_id,))
                actual_ms_iter = iter(actual_ms)
            else:
                actual_ms_iter = None

            def assert_is_equal(x, y):
                if x != y:
                    import pdb; pdb.set_trace()
            db_to_rev = self._db_id_to_rev_id
            for node in merge_sorted:
                try:
                    node.key = (db_to_rev[node.key],)
                except KeyError: # Look this one up in the db
                    rev_res = self._cursor.execute(
                        "SELECT revision_id FROM revision WHERE db_id = ?",
                        (node.key,)).fetchone()
                    rev_id = rev_res[0]
                    self._db_id_to_rev_id[node.key] = rev_id
                    self._rev_id_to_db_id[rev_id] = node.key
                    node.key = (rev_id,)
                if actual_ms_iter is None:
                    continue
                actual_node = actual_ms_iter.next()
                assert_is_equal(node.key, actual_node.key)
                assert_is_equal(node.revno, actual_node.revno)
                assert_is_equal(node.merge_depth, actual_node.merge_depth)
                assert_is_equal(node.end_of_merge, actual_node.end_of_merge)
            if actual_ms_iter is not None:
                try:
                    actual_node = actual_ms_iter.next()
                except StopIteration:
                    # no problem they both say they've finished
                    pass
                else:
                    # The next revision must have already been imported
                    assert self._is_imported(actual_node.key[0])
        else:
            merge_sorted = self._graph.merge_sort((tip_revision_id,))
        return merge_sorted

    def _import_tip(self, tip_revision_id, suppress_progress_and_commit=False):
        if suppress_progress_and_commit:
            pb = None
        else:
            pb = ui.ui_factory.nested_progress_bar()
        if pb is not None:
            pb.update('getting merge_sorted')
        merge_sorted = self._get_merge_sorted_tip(tip_revision_id)
        if not self._incremental:
            # If _incremental all the revisions will have already been ensured
            # by the _update_ancestry code.
            if pb is not None:
                pb.update('allocating revisions', 0,
                          len(merge_sorted))
            self._ensure_revisions([n.key[0] for n in merge_sorted])
            if pb is not None:
                pb.update('updating parents', 0,
                          len(merge_sorted))
            self._update_parents(merge_sorted)
        try:
            last_mainline_rev_id = None
            new_nodes = []
            for idx, node in enumerate(merge_sorted):
                if pb is not None and idx & 0xFF == 0:
                    pb.update('importing', idx, len(merge_sorted))
                if last_mainline_rev_id is None:
                    assert not new_nodes
                    assert node.merge_depth == 0, \
                        "We did not start at a mainline?"
                    last_mainline_rev_id = node.key[0]
                    new_nodes.append(node)
                    continue
                if node.merge_depth == 0:
                    # We're at a new mainline. Insert the nodes for the
                    # previous mainline. If this has already been inserted, we
                    # assume all previous ones are also. Safe as long as we
                    # wait to commit() until we insert all parents.
                    if not self._insert_nodes(last_mainline_rev_id, new_nodes):
                        # This data has already been imported.
                        new_nodes = []
                        break
                    last_mainline_rev_id = node.key[0]
                    new_nodes = []
                new_nodes.append(node)
            if new_nodes:
                assert last_mainline_rev_id is not None
                self._insert_nodes(last_mainline_rev_id, new_nodes)
                new_nodes = []
            self._build_one_mainline(tip_revision_id)
        finally:
            if pb is not None:
                pb.finished()
        return merge_sorted

    def _update_ancestry(self, new_tip_rev_id):
        """Walk the parents of this tip, updating 'revision' and 'parent'

        self._rev_id_to_db_id will be updated.
        """
        (known, parent_map,
         children) = self._find_known_ancestors(new_tip_rev_id)
        self._compute_gdfo_and_insert(known, children, parent_map)
        self._insert_parent_map(parent_map)
        # This seems to slow things down a fair amount. On bzrtools, we end up
        # calling it 75 times, and it ends up taking 800ms. vs a total rutime
        # of 1200ms otherwise.
        # self._db_conn.commit()

    def _find_known_ancestors(self, new_tip_rev_id):
        """Starting at tip, find ancestors we already have"""
        needed = [new_tip_rev_id]
        all_needed = set(new_tip_rev_id)
        children = {}
        parent_map = {}
        known = {}
        pb = ui.ui_factory.nested_progress_bar()
        try:
            while needed:
                pb.update('Finding ancestry', len(all_needed), len(all_needed))
                rev_id = needed.pop()
                if rev_id in known:
                    # We may add particular parents multiple times, just ignore
                    # them once they've been found
                    continue
                res = self._cursor.execute(
                    "SELECT gdfo FROM revision WHERE revision_id = ?",
                    [rev_id]).fetchone()
                if res is not None:
                    known[rev_id] = res[0]
                    continue
                # We don't have this entry recorded yet, add the parents to the
                # search
                pmap = self._branch.repository.get_parent_map([rev_id])
                parent_map.update(pmap)
                parent_ids = pmap.get(rev_id, None)
                if parent_ids is None or parent_ids == NULL_PARENTS:
                    # We can insert this rev directly, because we know its
                    # gdfo, as it has no parents.
                    parent_map[rev_id] = ()
                    self._cursor.execute("INSERT INTO revision (revision_id, gdfo)"
                                         " VALUES (?, ?)", (rev_id, 1))
                    # Wrap around to populate known quickly
                    needed.append(rev_id)
                    if parent_ids is None:
                        # This is a ghost, add it to the table
                        self._cursor.execute("INSERT INTO ghost (db_id)"
                                             " SELECT db_id FROM revision"
                                             "  WHERE revision_id = ?",
                                             (rev_id,))
                    continue
                for parent_id in pmap[rev_id]:
                    if parent_id not in known:
                        if parent_id not in all_needed:
                            needed.append(parent_id)
                            all_needed.add(parent_id)
                    children.setdefault(parent_id, []).append(rev_id)
        finally:
            pb.finished()
        return known, parent_map, children

    def _compute_gdfo_and_insert(self, known, children, parent_map):
        # At this point, we should have walked to all known parents, and should
        # be able to build up the gdfo and parent info for all keys.
        pending = [(gdfo, rev_id) for rev_id, gdfo in known.iteritems()]
        while pending:
            gdfo, rev_id = pending.pop()
            for child_id in children.get(rev_id, []):
                if child_id in known:
                    # XXX: Already numbered?
                    assert known[child_id] > gdfo
                    continue
                parent_ids = parent_map[child_id]
                max_gdfo = -1
                for parent_id in parent_ids:
                    try:
                        this_gdfo = known[parent_id]
                    except KeyError:
                        # One parent hasn't been computed yet
                        break
                    if this_gdfo > max_gdfo:
                        max_gdfo = this_gdfo
                else:
                    # All parents have their gdfo known
                    # assert gdfo == max_gdfo
                    child_gdfo = max_gdfo + 1
                    known[child_id] = child_gdfo
                    self._cursor.execute(
                        "INSERT INTO revision (revision_id, gdfo)"
                        " VALUES (?, ?)",
                        (child_id, child_gdfo))
                    # Put this into the pending queue so that *its* children
                    # also get updated
                    pending.append((child_gdfo, child_id))
        if self._graph is not None:
            for rev_id, gdfo in known.iteritems():
                assert gdfo == self._graph._nodes[(rev_id,)].gdfo

    def _get_db_id(self, revision_id):
        db_res = self._cursor.execute('SELECT db_id FROM revision'
                                      ' WHERE revision_id = ?',
                                      [revision_id]).fetchone()
        if db_res is None:
            return None
        return db_res[0]

    def _update_dotted(self, new_tip_rev_id):
        """We have a new 'tip' revision, Update the dotted_revno table."""
        # Just make sure the db has valid info for all the existing entries
        self._update_ancestry(new_tip_rev_id)

    def _get_mainline_range_count(self, head_db_id):
        """Does the given head_db_id already have a range defined using it."""
        res = self._cursor.execute("SELECT pkey, count, tail"
                                   " FROM mainline_parent_range"
                                   " WHERE head = ?"
                                   " ORDER BY count DESC LIMIT 1",
                                   [head_db_id]).fetchone()
        if res is None:
            return None, None, None
        return res

    def _get_mainline_range(self, range_key):
        """Get the revisions in the mainline range specified."""
        res = self._cursor.execute("SELECT revision FROM mainline_parent"
                                   " WHERE range = ?"
                                   " ORDER BY dist DESC", [range_key])
        return [r[0] for r in res.fetchall()]

    def _get_lh_parent_db_id(self, revision_db_id):
        parent_res = self._cursor.execute("""
            SELECT parent.parent
              FROM parent
             WHERE parent.child = ?
               AND parent_idx = 0
            LIMIT 1 -- hint to the db, should always be only 1
            """, (revision_db_id,)).fetchone()
        # self._stats['lh_parent_step'] += 1
        if parent_res is None:
            return None
        return parent_res[0]

    def _insert_range(self, range_db_ids, tail_db_id):
        head_db_id = range_db_ids[0]
        self._cursor.execute("INSERT INTO mainline_parent_range"
                             " (head, tail, count) VALUES (?, ?, ?)",
                             (head_db_id, tail_db_id, len(range_db_ids)))
        # Note: This works for sqlite, does it work for pgsql?
        range_key = self._cursor.lastrowid
        self._stats['ranges_inserted'] += 1
        # Note that 'tail' is explicitly not included in the range
        self._stats['revs_in_ranges'] += len(range_db_ids)
        self._cursor.executemany(
            "INSERT INTO mainline_parent (range, revision, dist)"
            " VALUES (?, ?, ?)",
            [(range_key, d, idx) for idx, d in enumerate(range_db_ids)])

    def _build_one_mainline(self, head_rev_id):
        # 1) Walk backward until you find an existing entry in the
        #    mainline_parent_range table (or you reach the end)
        # 2) If the range has less than X revisions, include it in the
        #    revisions to be added
        # 3) chop the list into X revision sections, and insert them
        #
        # This should ensure that any given ancestry has at most 1 section
        # which has less than X revisions, and it should preserve convergence.
        self._ensure_revisions([head_rev_id])
        cur_db_id = self._rev_id_to_db_id[head_rev_id]
        range_db_ids = []
        while cur_db_id is not None:
            (range_key, next_count,
             tail) = self._get_mainline_range_count(cur_db_id)
            if range_key is not None:
                # This tip is already present in mainline_parent_range
                # table.
                if (range_db_ids
                    and next_count < self._MAINLINE_PARENT_RANGE_LEN):
                    range_db_ids.extend(self._get_mainline_range(range_key))
                    cur_db_id = tail
                break
            else:
                range_db_ids.append(cur_db_id)
                cur_db_id = self._get_lh_parent_db_id(cur_db_id)
        # We now have a list of db ids that need to be split up into
        # ranges.
        while range_db_ids:
            tail_db_ids = range_db_ids[-self._MAINLINE_PARENT_RANGE_LEN:]
            del range_db_ids[-self._MAINLINE_PARENT_RANGE_LEN:]
            self._insert_range(tail_db_ids, cur_db_id)
            cur_db_id = tail_db_ids[0]


class _MergeSortNode(object):
    """A simple object that represents one entry in the merge sorted graph."""

    __slots__ = ('key', 'merge_depth', 'revno', 'end_of_merge',
                 '_left_parent', '_left_pending_parent',
                 '_pending_parents', '_is_first',
                 )

    def __init__(self, key, merge_depth, left_parent, pending_parents,
                 is_first):
        self.key = key
        self.merge_depth = merge_depth
        self.revno = None
        self.end_of_merge = None
        self._left_parent = left_parent
        self._left_pending_parent = left_parent
        self._pending_parents = pending_parents
        self._is_first = is_first

    def __repr__(self):
        return '%s(%s, %s, %s, %s [%s %s %s %s])' % (
            self.__class__.__name__,
            self.key, self.revno, self.end_of_merge, self.merge_depth,
            self._left_parent, self._left_pending_parent,
            self._pending_parents, self._is_first)


class _IncrementalMergeSort(object):
    """Context for importing partial history."""
    # Note: all of the ids in this object are database ids. the revision_ids
    #       should have already been imported before we get to this step.

    def __init__(self, importer, tip_db_id):
        self._importer = importer
        self._tip_db_id = tip_db_id
        self._mainline_db_ids = None
        self._imported_mainline_id = None
        self._cursor = importer._cursor
        self._stats = importer._stats

        # db_id => gdfo
        self._known_gdfo = {}
        # db_ids that we know are ancestors of mainline_db_ids that are not
        # ancestors of pre_mainline_id
        self._interesting_ancestor_ids = set()

        # Information from the dotted_revno table for revisions that are in the
        # already-imported mainline.
        self._imported_dotted_revno = {}
        # What dotted revnos have been loaded
        self._known_dotted = set()
        # This is the gdfo of the current mainline revision search tip. This is
        # the threshold such that 
        self._imported_gdfo = None

        # Revisions that we are walking, to see if they are interesting, or
        # already imported.
        self._search_tips = None
        # mainline revno => number of child branches
        self._revno_to_branch_count = {}
        # (revno, branch_num) => oldest seen child
        self._branch_to_child_count = {}

        self._depth_first_stack = None
        self._scheduled_stack = None
        self._seen_parents = None
        # Map from db_id => parent_ids
        self._parent_map = self._importer._db_parent_map

        # We just populate all known ghosts here.
        # TODO: Ghosts are expected to be rare. If we find a case where probing
        #       for them at runtime is better than grabbing them all at once,
        #       re-evaluate this decision.
        self._ghosts = None

    def _find_needed_mainline(self):
        """Find mainline revisions that need to be filled out.
        
        :return: ([mainline_not_imported], most_recent_imported)
        """
        db_id = self._tip_db_id
        needed = []
        while db_id is not None and not self._is_imported_db_id(db_id):
            needed.append(db_id)
            db_id = self._importer._get_lh_parent_db_id(db_id)
        self._mainline_db_ids = needed
        self._interesting_ancestor_ids.update(self._mainline_db_ids)
        self._imported_mainline_id = db_id

    def _get_initial_search_tips(self):
        """Grab the right-hand parents of all the interesting mainline.

        We know we already searched all of the left-hand parents, so just grab
        the right-hand parents.
        """
        # TODO: Split this into a loop, since sqlite has a maximum number of
        #       parameters.
        res = _get_result_for_many(self._cursor,
            "SELECT parent, gdfo FROM parent, revision"
            " WHERE parent.parent = revision.db_id"
            "   AND parent_idx != 0"
            "   AND child IN (%s)",
            self._mainline_db_ids)
        self._search_tips = set([r[0] for r in res])
        self._stats['num_search_tips'] += len(self._search_tips)
        self._known_gdfo.update(res)
        # We know that we will eventually need at least 1 step of the mainline
        # (it gives us the basis for numbering everything). We do it now,
        # because it increases the 'cheap' filtering we can do right away.
        self._stats['step mainline initial'] += 1
        self._step_mainline()
        ghost_res = self._cursor.execute("SELECT db_id FROM ghost").fetchall()
        self._ghosts = set([g[0] for g in ghost_res])

    def _is_imported_db_id(self, tip_db_id):
        res = self._cursor.execute(
            "SELECT count(*) FROM dotted_revno"
            " WHERE tip_revision = ?"
            "   AND tip_revision = merged_revision",
            (tip_db_id,)).fetchone()
        return res[0] > 0

    def _split_search_tips_by_gdfo(self, unknown):
        """For these search tips, mark ones 'interesting' based on gdfo.
        
        All search tips are ancestors of _mainline_db_ids. So if their gdfo
        indicates that they could not be merged into already imported
        revisions, then we know automatically that they are
        new-and-interesting. Further, if they are present in
        _imported_dotted_revno, then we know they are not interesting, and
        we will stop searching them.

        Otherwise, we don't know for sure which category they fall into, so
        we return them for further processing.

        :return: still_unknown - search tips that aren't known to be
            interesting, and aren't known to be in the ancestry of already
            imported revisions.
        """
        still_unknown = []
        min_gdfo = None
        for db_id in unknown:
            if (db_id in self._imported_dotted_revno
                or db_id == self._imported_mainline_id):
                # This should be removed as a search tip, we know it isn't
                # interesting, it is an ancestor of an imported revision
                self._stats['split already imported'] += 1
                self._search_tips.remove(db_id)
                continue
            gdfo = self._known_gdfo[db_id]
            if gdfo >= self._imported_gdfo:
                self._stats['split gdfo'] += 1
                self._interesting_ancestor_ids.add(db_id)
            else:
                still_unknown.append(db_id)
        return still_unknown

    def _split_interesting_using_children(self, unknown_search_tips):
        """Find children of these search tips.

        For each search tip, we find all of its known children. We then filter
        the children by:
            a) child is ignored if child in _interesting_ancestor_ids
            b) child is ignored if gdfo(child) > _imported_gdfo
                or (gdfo(child) == _imported_gdfo and child !=
                _imported_mainline_id)
               The reason for the extra check is because for the ancestry
               left-to-be-searched, with tip at _imported_mainline_id, *only*
               _imported_mainline_id is allowed to have gdfo = _imported_gdfo.
        for each search tip, if there are no interesting children, then this
        search tip is definitely interesting (there is no path for it to be
        merged into a previous mainline entry.)

        :return: still_unknown
            still_unknown are the search tips that are still have children that
            could be possibly merged.
        """
        interesting = self._interesting_ancestor_ids
        parent_child_res = self._cursor.execute(_add_n_params(
            "SELECT parent, child FROM parent"
            " WHERE parent IN (%s)",
            len(unknown_search_tips)), unknown_search_tips).fetchall()
        parent_to_children = {}
        already_imported = set()
        for parent, child in parent_child_res:
            if (child in self._imported_dotted_revno
                or child == self._imported_mainline_id):
                # This child is already imported, so obviously the parent is,
                # too.
                self._stats['split child imported'] += 1
                already_imported.add(parent)
                already_imported.add(child)
            parent_to_children.setdefault(parent, []).append(child)
        self._search_tips.difference_update(already_imported)
        possibly_merged_children = set(
            [c for p, c in parent_child_res
                if c not in interesting and p not in already_imported])
        known_gdfo = self._known_gdfo
        unknown_gdfos = [c for c in possibly_merged_children
                            if c not in known_gdfo]
        # TODO: Is it more wasteful to join this table early, or is it better
        #       because we avoid having to pass N parameters back in?
        # TODO: Would sorting the db ids help? They are the primary key for the
        #       table, so we could potentially fetch in a better order.
        if unknown_gdfos:
            res = self._cursor.execute(_add_n_params(
                "SELECT db_id, gdfo FROM revision WHERE db_id IN (%s)",
                len(unknown_gdfos)), unknown_gdfos)
            known_gdfo.update(res)
        min_gdfo = self._imported_gdfo
        # Remove all of the children who have gdfo >= min. We already handled
        # the == min case in the first loop.
        possibly_merged_children.difference_update(
            [c for c in possibly_merged_children if known_gdfo[c] >= min_gdfo])
        still_unknown = []
        for parent in unknown_search_tips:
            if parent in already_imported:
                self._stats['split parent imported'] += 1
                continue
            for c in parent_to_children[parent]:
                if c in possibly_merged_children:
                    still_unknown.append(parent)
                    break
            else: # All children could not be possibly merged
                self._stats['split children interesting'] += 1
                interesting.add(parent)
        return still_unknown

    def _step_mainline(self):
        """Move the mainline pointer by one, updating the data."""
        self._stats['step mainline'] += 1
        res = self._cursor.execute(
            "SELECT merged_revision, revno, end_of_merge, merge_depth"
            "  FROM dotted_revno WHERE tip_revision = ? ORDER BY dist",
            [self._imported_mainline_id]).fetchall()
        stuple = static_tuple.StaticTuple.from_sequence
        st = static_tuple.StaticTuple
        dotted_info = [st(r[0], st(stuple(map(int, r[1].split('.'))),
                                   r[2], r[3]))
                       for r in res]
        self._stats['step mainline cache missed'] += 1
        self._stats['step mainline added'] += len(dotted_info)
        self._update_info_from_dotted_revno(dotted_info)
        # TODO: We could remove search tips that show up as newly merged
        #       though that can wait until the next
        #       _split_search_tips_by_gdfo
        # new_merged_ids = [r[0] for r in res]
        res = self._cursor.execute("SELECT parent, gdfo"
                                   "  FROM parent, revision"
                                   " WHERE parent = db_id"
                                   "   AND parent_idx = 0"
                                   "   AND child = ?",
                                   [self._imported_mainline_id]).fetchone()
        if res is None:
            # Walked off the mainline...
            # TODO: Make sure this stuff is tested
            self._imported_mainline_id = None
            self._imported_gdfo = 0
        else:
            self._imported_mainline_id, self._imported_gdfo = res
            self._known_gdfo[self._imported_mainline_id] = self._imported_gdfo

    def _step_search_tips(self):
        """Move the search tips to their parents."""
        self._stats['step search tips'] += 1
        res = _get_result_for_many(self._cursor,
            "SELECT parent, gdfo FROM parent, revision"
            " WHERE parent=db_id AND child IN (%s)",
            list(self._search_tips))
        # TODO: We could use this time to fill out _parent_map, rather than
        #       waiting until _push_node and duplicating a request to the
        #       parent table. It may be reasonable to wait on gdfo also...

        # Filter out search tips that we've already searched via a different
        # path. By construction, if we are stepping the search tips, we know
        # that all previous search tips are either in
        # self._imported_dotted_revno or in self._interesting_ancestor_ids.
        # _imported_dotted_revno will be filtered in the first
        # _split_search_tips_by_gdfo call, so we just filter out already
        # interesting ones.
        interesting = self._interesting_ancestor_ids
        self._search_tips = set([r[0] for r in res if r[0] not in interesting])
        # TODO: For search tips we will be removing, we don't need to join
        #       against revision since we should already have them. There may
        #       be other ways that we already know gdfo. It may be cheaper to
        #       check first.
        self._stats['num_search_tips'] += len(self._search_tips)
        self._known_gdfo.update(res)

    def _ensure_lh_parent_info(self):
        """LH parents of interesting_ancestor_ids is either present or pending.

        Either the data should be in _imported_dotted_revno, or the lh parent
        should be in interesting_ancestor_ids (meaning we will number it).
        """
        #XXX REMOVE: pmap = self._parent_map
        missing_parent_ids = set()
        for db_id in self._interesting_ancestor_ids:
            parent_ids = self._get_parents(db_id)
            if not parent_ids: # no parents, nothing to add
                continue
            lh_parent = parent_ids[0]
            if lh_parent in self._interesting_ancestor_ids:
                continue
            if lh_parent in self._imported_dotted_revno:
                continue
            missing_parent_ids.add(lh_parent)
        missing_parent_ids.difference_update(self._ghosts)
        while missing_parent_ids:
            self._stats['step mainline ensure LH'] += 1
            self._step_mainline()
            missing_parent_ids = missing_parent_ids.difference(
                                    self._imported_dotted_revno)

    def _find_interesting_ancestry(self):
        self._find_needed_mainline()
        self._get_initial_search_tips()
        while self._search_tips:
            # We don't know whether these search tips are known interesting, or
            # known uninteresting
            unknown = list(self._search_tips)
            while unknown:
                unknown = self._split_search_tips_by_gdfo(unknown)
                if not unknown:
                    break
                unknown = self._split_interesting_using_children(unknown)
                if not unknown:
                    break
                # The current search tips are the 'newest' possible tips right
                # now. If we can't classify them as definitely being
                # interesting, then we need to step the mainline until we can.
                # This means that the current search tips have children that
                # could be merged into an earlier mainline, walk the mainline
                # to see if we can resolve that.
                # Note that relying strictly on gdfo is a bit of a waste here,
                # because you may have a rev with 10 children before it lands
                # in mainline, but all 11 revs will be in the dotted_revno
                # cache for that mainline.
                self._stats['step mainline unknown'] += 1
                self._step_mainline()
            # All search_tips are known to either be interesting or
            # uninteresting. Walk any search tips that remain.
            self._step_search_tips()
        # We're now sure we have all of the now-interesting revisions. To
        # number them, we need their left-hand parents to be in
        # _imported_dotted_revno
        self._ensure_lh_parent_info()

    def _update_info_from_dotted_revno(self, dotted_info):
        """Update info like 'child_seen' from the dotted_revno info."""
        # TODO: We can move this iterator into a parameter, and have it
        #       continuously updated from _step_mainline()
        self._imported_dotted_revno.update(dotted_info)
        self._known_dotted.update([i[1][0] for i in dotted_info])
        for db_id, (revno, eom, depth) in dotted_info:
            if len(revno) > 1: # dotted revno, make sure branch count is right
                base_revno = revno[0]
                if (base_revno not in self._revno_to_branch_count
                    or revno[1] > self._revno_to_branch_count[base_revno]):
                    self._revno_to_branch_count[base_revno] = revno[1]
                branch_key = revno[:2]
                mini_revno = revno[2]
            else:
                # *mainline* branch
                branch_key = 0
                mini_revno = revno[0]
                # We found a mainline branch, make sure it is marked as such
                self._revno_to_branch_count.setdefault(0, 0)
            if (branch_key not in self._branch_to_child_count
                or mini_revno > self._branch_to_child_count[branch_key]):
                self._branch_to_child_count[branch_key] = mini_revno

    def _is_first_child(self, parent_id):
        """Is this the first child seen for the given parent?"""
        if parent_id in self._seen_parents:
            return False
        # We haven't seen this while walking, but perhaps the already merged
        # stuff has.
        self._seen_parents.add(parent_id)
        if parent_id not in self._imported_dotted_revno:
            # Haven't seen this parent merged before, so we can't have seen
            # children of it
            return True
        revno = self._imported_dotted_revno[parent_id][0]
        if len(revno) > 1:
            branch_key = revno[:2]
            mini_revno = revno[2]
        else:
            branch_key = 0
            mini_revno = revno[0]
        if self._branch_to_child_count.get(branch_key, 0) > mini_revno:
            # This revision shows up in the graph, but another revision in this
            # branch shows up later, so this revision must have already been
            # seen
            return False
        # If we got this far, it doesn't appear to have been seen.
        return True

    def _get_parents(self, db_id):
        if db_id in self._parent_map:
            parent_ids = self._parent_map[db_id]
        else:
            parent_res = self._cursor.execute(
                        "SELECT parent FROM parent WHERE child = ?"
                        " ORDER BY parent_idx", (db_id,)).fetchall()
            parent_ids = static_tuple.StaticTuple.from_sequence(
                [r[0] for r in parent_res])
            self._parent_map[db_id] = parent_ids
        return parent_ids

    def _push_node(self, db_id, merge_depth):
        # TODO: Check if db_id is a ghost (not allowed on the stack)
        self._stats['pushed'] += 1
        if db_id not in self._interesting_ancestor_ids:
            # This is a parent that we really don't need to number
            self._stats['pushed uninteresting'] += 1
            return
        parent_ids = self._get_parents(db_id)
        if len(parent_ids) <= 0:
            left_parent = None
            # We are dealing with a 'new root' possibly because of a ghost,
            # possibly because of merging a new ancestry.
            # KnownGraph.merge_sort just always says True here, so stick with
            # that
            is_first = True
        else:
            left_parent = parent_ids[0]
            if left_parent in self._ghosts:
                left_parent = None
                is_first = True
            else:
                is_first = self._is_first_child(left_parent)
        # Note: we don't have to filter out self._ghosts here, as long as we do
        #       it in _push_node
        pending_parents = static_tuple.StaticTuple.from_sequence(
            [p for p in parent_ids[1:] if p not in self._ghosts])
        # v- logically probably better as a tuple or object. We currently
        # modify it in place, so we use a list
        self._depth_first_stack.append(
            _MergeSortNode(db_id, merge_depth, left_parent, pending_parents,
                           is_first))

    def _step_to_latest_branch(self, base_revno):
        """Step the mainline until we've loaded the latest sub-branch.

        This is used when we need to create a new child branch. We need to
        ensure that we've loaded the most-recently-merged branch, so that we
        can generate the correct branch counter.

        For example, if you have a revision whose left-hand parent is 1.2.3,
        you need to load mainline revisions until you find some revision like
        (1.?.1). This will ensure that you have the most recent branch merged
        to mainline that was branched from the revno=1 revision in mainline.
        
        Note that if we find (1,3,1) before finding (1,2,1) that is ok. As long
        as we have found the first revision of any sub-branch, we know that
        we've found the most recent (since we walk backwards).

        :param base_revno: The revision that this branch is based on. 0 means
            that this is a new-root branch.
        :return: None
        """
        self._stats['step to latest'] += 1
        step_count = 0
        start_point = self._imported_mainline_id
        found = None
        while self._imported_mainline_id is not None:
            if (base_revno,) in self._known_dotted:
                # We have walked far enough to load the original revision,
                # which means we've loaded all children.
                self._stats['step to latest found base'] += 1
                found = (base_revno,)
                break
            # Estimate what is the most recent branch, and see if we have read
            # its first revision
            branch_count = self._revno_to_branch_count.get(base_revno, 0)
            root_of_branch_revno = (base_revno, branch_count, 1)
            # Note: if branch_count == 0, that means we haven't seen any
            #       other branches for this revision.
            if root_of_branch_revno in self._known_dotted:
                found = root_of_branch_revno
                break
            self._stats['step mainline to-latest'] += 1
            if base_revno == 0:
                self._stats['step mainline to-latest NULL'] += 1
            self._step_mainline()
            step_count += 1

    def _pop_node(self):
        """Move the last node from the _depth_first_stack to _scheduled_stack.

        This is the most left-hand node that we are able to find.
        """
        node = self._depth_first_stack.pop()
        if node._left_parent is not None:
            parent_revno = self._imported_dotted_revno[node._left_parent][0]
            if node._is_first: # We simply number as parent + 1
                if len(parent_revno) == 1:
                    mini_revno = parent_revno[0] + 1
                    revno = (mini_revno,)
                    # Not sure if we *have* to maintain it, but it does keep
                    # our data-structures consistent
                    if mini_revno > self._branch_to_child_count[0]:
                        self._branch_to_child_count[0] = mini_revno
                else:
                    revno = parent_revno[:2] + (parent_revno[2] + 1,)
            else:
                # we need a new branch number. To get this correct, we have to
                # make sure that the beginning of this branch has been loaded
                if len(parent_revno) > 1:
                    # if parent_revno is a mainline, then
                    # _ensure_lh_parent_info should have already loaded enough
                    # data. So we only do this when the parent is a merged
                    # revision.
                    self._step_to_latest_branch(parent_revno[0])
                base_revno = parent_revno[0]
                branch_count = (
                    self._revno_to_branch_count.get(base_revno, 0) + 1)
                self._revno_to_branch_count[base_revno] = branch_count
                revno = (base_revno, branch_count, 1)
        else:
            # We found a new root. There are 2 cases:
            #   a) This is the very first revision in the branch. In which case
            #      self._revno_to_branch_count won't have any counts for
            #      'revno' 0.
            #   b) This is a ghost / the first revision in a branch that was
            #      merged. We need to allocate a new branch number.
            #   This distinction is pretty much the same as the 'is_first'
            #   check for revs with a parent if you consider the NULL revision
            #   to be revno 0.
            #   We have to walk back far enough to be sure that we have the
            #   most-recent merged new-root. This can be detected by finding
            #   any new root's first revision. And, of course, we should find
            #   the last one first while walking backwards.
            #   Theory:
            #       When you see (0,X,1) you've reached the point where the X
            #       number was chosen. A hypothetical (0,X+1,1) revision would
            #       only be numbered X+1 if it was merged after (0,X,1). Thus
            #       the *first* (0,?,1) revision you find merged must be the
            #       last.

            self._step_to_latest_branch(0)
            branch_count = self._revno_to_branch_count.get(0, -1) + 1
            self._revno_to_branch_count[0] = branch_count
            if branch_count == 0: # This is the mainline
                revno = (1,)
                self._branch_to_child_count[0] = 1
            else:
                revno = (0, branch_count, 1)
        if not self._scheduled_stack:
            # For all but mainline revisions, we break on the end-of-merge. So
            # when we start new numbering, end_of_merge is True. For mainline
            # revisions, this is only true when we don't have a parent.
            end_of_merge = True
            if node._left_parent is not None and node.merge_depth == 0:
                end_of_merge = False
        else:
            prev_node = self._scheduled_stack[-1]
            if prev_node.merge_depth < node.merge_depth:
                end_of_merge = True
            elif (prev_node.merge_depth == node.merge_depth
                  and prev_node.key not in self._parent_map[node.key]):
                # Next node is not a direct parent
                end_of_merge = True
            else:
                end_of_merge = False
        revno = static_tuple.StaticTuple.from_sequence(revno)
        node.revno = revno
        node.end_of_merge = end_of_merge
        self._imported_dotted_revno[node.key] = static_tuple.StaticTuple(
            revno, end_of_merge, node.merge_depth)
        self._known_dotted.add(revno)
        node._pending_parents = None
        self._scheduled_stack.append(node)

    def _compute_merge_sort(self):
        self._depth_first_stack = []
        self._scheduled_stack = []
        self._seen_parents = set()
        if not self._mainline_db_ids:
            # Nothing to number
            return
        self._push_node(self._mainline_db_ids[0], 0)

        while self._depth_first_stack:
            last = self._depth_first_stack[-1]
            if last._left_pending_parent is None and not last._pending_parents:
                # The parents have been processed, pop the node
                self._pop_node()
                continue
            while (last._left_pending_parent is not None
                   or last._pending_parents):
                if last._left_pending_parent is not None:
                    # Push on the left-hand-parent
                    next_db_id = last._left_pending_parent
                    last._left_pending_parent = None
                else:
                    pending_parents = last._pending_parents
                    next_db_id = pending_parents[-1]
                    last._pending_parents = pending_parents[:-1]
                if next_db_id in self._imported_dotted_revno:
                    continue
                if next_db_id == last._left_parent: #Is the left-parent?
                    next_merge_depth = last.merge_depth
                else:
                    next_merge_depth = last.merge_depth + 1
                self._push_node(next_db_id, next_merge_depth)
                # And switch to the outer loop
                break

    def topo_order(self):
        self._find_interesting_ancestry()
        self._compute_merge_sort()
        return list(reversed(self._scheduled_stack))


class Querier(object):
    """Perform queries on an existing history db."""

    def __init__(self, db_path, a_branch):
        self._db_path = db_path
        self._db_conn = None
        self._cursor = None
        self._importer_lock = None
        self._branch = a_branch
        self._branch_tip_rev_id = a_branch.last_revision()
        self._branch_tip_db_id = self._get_db_id(self._branch_tip_rev_id)
        self._tip_is_imported = False
        self._stats = defaultdict(lambda: 0)

    def set_importer_lock(self, lock):
        """Add a thread-lock for building and running an Importer.

        The DB back-end is generally single-writer, so add a thread lock to
        avoid having two writers trying to access it at the same time.

        This will be used as part of _import_tip. Note that it doesn't (yet?)
        support anything like timeout.
        """
        self._importer_lock = lock

    def _get_cursor(self):
        if self._cursor is not None:
            return self._cursor
        db_conn = dbapi2.connect(self._db_path)
        self._db_conn = db_conn
        self._cursor = self._db_conn.cursor()
        return self._cursor

    def ensure_branch_tip(self):
        """Ensure that the branch tip has been imported.

        This will run Importer if it has not.
        """
        if self._branch_tip_db_id is not None and self._tip_is_imported:
            return
        if self._branch_tip_db_id is None:
            # This revision has not been seen by the DB, so we know it isn't
            # imported
            self._import_tip()
            return
        if self._is_imported_db_id(self._branch_tip_db_id):
            # This revision was seen, and imported
            self._tip_is_imported = True
            return
        self._import_tip()

    def _import_tip(self):
        if self._cursor is not None:
            self.close()
        if self._importer_lock is not None:
            self._importer_lock.acquire()
        try:
            t = time.time()
            importer = Importer(self._db_path, self._branch,
                                tip_revision_id=self._branch_tip_rev_id,
                                incremental=True)
            importer.do_import()
            tdelta = time.time() - t
            if 'history_db' in debug.debug_flags:
                trace.note('imported %d nodes on-the-fly in %.3fs'
                           % (importer._stats.get('total_nodes_inserted', 0),
                              tdelta))
            self._db_conn = importer._db_conn
            self._cursor = importer._cursor
            self._branch_tip_db_id = self._get_db_id(self._branch_tip_rev_id)
            self._tip_is_imported = True
        finally:
            if self._importer_lock is not None:
                self._importer_lock.release()

    def _is_imported_db_id(self, tip_db_id):
        res = self._get_cursor().execute(
            "SELECT count(*) FROM dotted_revno"
            " WHERE tip_revision = ?"
            "   AND tip_revision = merged_revision",
            (tip_db_id,)).fetchone()
        return res[0] > 0

    def close(self):
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None
            self._cursor = None

    def _get_db_id(self, revision_id):
        try:
            db_res = self._get_cursor().execute(
                'SELECT db_id FROM revision'
                ' WHERE revision_id = ?',
                [revision_id]).fetchone()
        except dbapi2.OperationalError:
            return None
        if db_res is None:
            return None
        return db_res[0]

    def get_lh_parent_rev_id(self, revision_id):
        parent_res = self._get_cursor().execute("""
            SELECT p.revision_id
              FROM parent, revision as c, revision as p
             WHERE parent.child = c.db_id
               AND parent.parent = p.db_id
               AND c.revision_id = ?
               AND parent_idx = 0
            """, (revision_id,)).fetchone()
        self._stats['lh_parent_step'] += 1
        if parent_res is None:
            return None
        return parent_res[0]

    def get_children(self, revision_id):
        """Returns all the children the db knows about for this revision_id.

        (we should probably try to filter it based on ancestry of
        self._branch_tip_rev_id...)
        """
        # One option for people who care, is to just have them turn around a
        # request for get_dotted_revnos(), and things that aren't there are not
        # in the ancestry.
        cursor = self._get_cursor()
        res = cursor.execute("SELECT c.revision_id"
                             "  FROM revision p, parent, revision c"
                             " WHERE child = c.db_id"
                             "   AND parent = p.db_id"
                             "   AND p.revision_id = ?",
                             (revid,)).fetchall()
        return [r[0] for r in res]

    def _get_lh_parent_db_id(self, revision_db_id):
        parent_res = self._get_cursor().execute("""
            SELECT parent.parent
              FROM parent
             WHERE parent.child = ?
               AND parent_idx = 0
            """, (revision_db_id,)).fetchone()
        self._stats['lh_parent_step'] += 1
        if parent_res is None:
            return None
        return parent_res[0]

    def _get_range_key_and_tail(self, tip_db_id):
        """Return the best range w/ head = tip_db_id or None."""
        range_res = self._get_cursor().execute(
            "SELECT pkey, tail"
            "  FROM mainline_parent_range"
            " WHERE head = ?"
            " ORDER BY count DESC LIMIT 1",
            (tip_db_id,)).fetchone()
        if range_res is None:
            tail = self._get_lh_parent_db_id(tip_db_id)
            return None, tail
        return range_res

    def get_dotted_revnos(self, revision_ids):
        """Determine the dotted revno, using the range info, etc."""
        self.ensure_branch_tip()
        t = time.time()
        cursor = self._get_cursor()
        tip_db_id = self._branch_tip_db_id
        if tip_db_id is None:
            return {}
        db_ids = set()
        db_id_to_rev_id = {}
        for rev_id in revision_ids:
            db_id = self._get_db_id(rev_id)
            if db_id is None:
                import pdb; pdb.set_trace()
            db_ids.add(db_id)
            db_id_to_rev_id[db_id] = rev_id
        revnos = {}
        while tip_db_id is not None and db_ids:
            self._stats['num_steps'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
            if range_key is None:
                revno_res = cursor.execute(_add_n_params(
                    "SELECT merged_revision, revno FROM dotted_revno"
                    " WHERE tip_revision = ?"
                    "   AND merged_revision IN (%s)",
                    len(db_ids)), 
                    [tip_db_id] + list(db_ids)).fetchall()
            else:
                revno_res = cursor.execute(_add_n_params(
                    "SELECT merged_revision, revno"
                    "  FROM dotted_revno, mainline_parent"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND mainline_parent.range = ?"
                    "   AND merged_revision IN (%s)",
                    len(db_ids)), 
                    [range_key] + list(db_ids)).fetchall()
            tip_db_id = next_db_id
            for db_id, revno in revno_res:
                db_ids.discard(db_id)
                revnos[db_id_to_rev_id[db_id]] = tuple(map(int,
                    revno.split('.')))
        self._stats['query_time'] += (time.time() - t)
        return revnos

    def get_revision_ids(self, revnos):
        """Map from a dotted-revno back into a revision_id."""
        self.ensure_branch_tip()
        t = time.time()
        tip_db_id = self._branch_tip_db_id
        # TODO: If tip_db_id is None, maybe we want to raise an exception here?
        #       To indicate that the branch has not been imported yet
        revno_strs = set(['.'.join(map(str, revno)) for revno in revnos])
        revno_map = {}
        cursor = self._get_cursor()
        while tip_db_id is not None and revno_strs:
            self._stats['num_steps'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
            if range_key is None:
                revision_res = cursor.execute(_add_n_params(
                    "SELECT revision_id, revno"
                    "  FROM dotted_revno, revision"
                    " WHERE merged_revision = revision.db_id"
                    "   AND tip_revision = ?"
                    "   AND revno IN (%s)", len(revno_strs)),
                    [tip_db_id] + list(revno_strs)).fetchall()
            else:
                revision_res = cursor.execute(_add_n_params(
                    "SELECT revision_id, revno"
                    "  FROM dotted_revno, mainline_parent, revision"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND merged_revision = revision.db_id"
                    "   AND mainline_parent.range = ?"
                    "   AND revno IN (%s)", len(revno_strs)),
                    [range_key] + list(revno_strs)).fetchall()
            tip_db_id = next_db_id
            for revision_id, revno_str in revision_res:
                dotted = tuple(map(int, revno_str.split('.')))
                revno_strs.discard(revno_str)
                revno_map[dotted] = revision_id
        self._stats['query_time'] += (time.time() - t)
        return revno_map

    def get_mainline_where_merged(self, revision_ids):
        """Determine what mainline revisions merged the given revisions."""
        self.ensure_branch_tip()
        t = time.time()
        tip_db_id = self._branch_tip_db_id
        if tip_db_id is None:
            return {}
        cursor = self._get_cursor()
        db_ids = set()
        db_id_to_rev_id = {}
        for rev_id in revision_ids:
            db_id = self._get_db_id(rev_id)
            if db_id is None:
                import pdb; pdb.set_trace()
            db_ids.add(db_id)
            db_id_to_rev_id[db_id] = rev_id
        revision_to_mainline_map = {}
        while tip_db_id is not None and db_ids:
            self._stats['num_steps'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
            if range_key is None:
                mainline_res = cursor.execute(_add_n_params(
                    "SELECT revision_id, merged_revision"
                    "  FROM dotted_revno, revision"
                    " WHERE tip_revision = ?"
                    "   AND tip_revision = revision.db_id"
                    "   AND merged_revision IN (%s)",
                    len(db_ids)), 
                    [tip_db_id] + list(db_ids)).fetchall()
            else:
                mainline_res = cursor.execute(_add_n_params(
                    "SELECT revision_id, merged_revision"
                    "  FROM dotted_revno, mainline_parent, revision"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND tip_revision = db_id"
                    "   AND mainline_parent.range = ?"
                    "   AND merged_revision IN (%s)",
                    len(db_ids)), 
                    [range_key] + list(db_ids)).fetchall()
            tip_db_id = next_db_id
            for mainline_revision_id, merged_db_id in mainline_res:
                db_ids.discard(merged_db_id)
                revision_to_mainline_map[db_id_to_rev_id[merged_db_id]] = \
                    mainline_revision_id
        self._stats['query_time'] += (time.time() - t)
        return revision_to_mainline_map

    def _get_mainline_range_starting_at(self, head_db_id):
        """Try to find a range at this tip.

        If a range cannot be found, just find the next parent.
        :return: (range_or_None, next_db_id)
        """
        cursor = self._get_cursor()
        range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
        if range_key is None:
            return None, next_db_id
        # TODO: Is ORDER BY dist ASC expensive? We know a priori that the list
        #       is probably already in sorted order, but does sqlite know that?
        range_db_ids = cursor.execute(
            "SELECT revision FROM mainline_parent"
            " WHERE range = ? ORDER BY dist ASC",
            (range_key,)).fetchall()
        db_ids = [r[0] for r in range_db_ids]
        return db_ids, next_db_id

    def walk_mainline(self):
        t = time.time()
        db_id = self._get_db_id(self._branch_tip_rev_id)
        all_ids = []
        while db_id is not None:
            self._stats['num_steps'] += 1
            next_range, next_db_id = self._get_mainline_range_starting_at(db_id)
            if next_range is None:
                # No range, so switch to using by-parent search
                all_ids.append(db_id)
            else:
                assert next_range[0] == db_id
                all_ids.extend(next_range)
            db_id = next_db_id
        self._stats['query_time'] += (time.time() - t)
        return all_ids

    def walk_ancestry(self):
        """Walk the whole ancestry.

        Use the information from the dotted_revno table and the mainline_parent
        table to speed things up.
        """
        db_id = self._get_db_id(self._branch_tip_rev_id)
        all_ancestors = set()
        t = time.time()
        cursor = self._get_cursor()
        while db_id is not None:
            self._stats['num_steps'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(db_id) 
            if range_key is None:
                merged_revs = cursor.execute(
                    "SELECT merged_revision FROM dotted_revno"
                    " WHERE tip_revision = ?",
                    (db_id,)).fetchall()
                all_ancestors.update([r[0] for r in merged_revs])
            else:
                merged_revs = cursor.execute(
                    "SELECT merged_revision FROM dotted_revno, mainline_parent"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND mainline_parent.range = ?",
                    [range_key]).fetchall()
                all_ancestors.update([r[0] for r in merged_revs])
            db_id = next_db_id
        self._stats['query_time'] += (time.time() - t)
        return all_ancestors

    def _find_tip_containing(self, tip_db_id, merged_db_id):
        """Walk backwards until you find the tip that contains the given id."""
        cursor = self._get_cursor()
        while tip_db_id is not None:
            if tip_db_id == merged_db_id:
                # A tip obviously contains itself
                self._stats['step_find_tip_as_merged'] += 1
                return tip_db_id
            self._stats['num_steps'] += 1
            self._stats['step_find_tip_containing'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
            if range_key is None:
                present_res = cursor.execute(
                    "SELECT 1 FROM dotted_revno"
                    " WHERE tip_revision = ?"
                    "   AND merged_revision = ?",
                    [tip_db_id, merged_db_id]).fetchone()
            else:
                present_res = cursor.execute(
                    "SELECT 1"
                    "  FROM dotted_revno, mainline_parent"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND mainline_parent.range = ?"
                    "   AND merged_revision = ?",
                    [range_key, merged_db_id]).fetchone()
            if present_res is not None:
                # We found a tip that contains merged_db_id
                return tip_db_id
            tip_db_id = next_db_id
        return None

    def _get_merge_sorted_range(self, tip_db_id, start_db_id, stop_db_id):
        """Starting at the given tip, read all merge_sorted data until stop."""
        if start_db_id is None or start_db_id == tip_db_id:
            found_start = True
        else:
            found_start = False
        cursor = self._get_cursor()
        while tip_db_id is not None:
            self._stats['num_steps'] += 1
            self._stats['step_get_merge_sorted'] += 1
            range_key, next_db_id = self._get_range_key_and_tail(tip_db_id)
            if range_key is None:
                merged_res = cursor.execute(
                    "SELECT db_id, revision_id, merge_depth, revno,"
                    "       end_of_merge"
                    "  FROM dotted_revno, revision"
                    " WHERE tip_revision = ?"
                    "   AND db_id = merged_revision"
                    " ORDER BY dist",
                    (tip_db_id,)).fetchall()
            else:
                # NOTE: Adding the ORDER BY costs us 981ms - 844ms = 137ms when
                #       doing 'bzr log -n0 -r -10..-1' on bzr.dev.
                #       That seems like a lot. Extracting them without sorting
                #       on them costs about the same amount. So the guess is
                #       that adding the extra columns requires more I/O.
                # At the moment, SELECT order == INSERT order, so we don't
                # strictly need it. I don't know that we can trust that,
                # though.
                merged_res = cursor.execute(
                    "SELECT db_id, revision_id, merge_depth, revno,"
                    "       end_of_merge"
                    # "       , mainline_parent.dist as mp_dist"
                    # "       , dotted_revno.dist as dr_dist"
                    "  FROM dotted_revno, revision, mainline_parent"
                    " WHERE tip_revision = mainline_parent.revision"
                    "   AND mainline_parent.range = ?"
                    "   AND db_id = merged_revision",
                    # " ORDER BY mainline_parent.dist, dotted_revno.dist",
                    [range_key]).fetchall()
            if found_start:
                for db_id, r_id, depth, revno_str, eom in merged_res:
                    if stop_db_id is not None and db_id == stop_db_id:
                        return
                    revno = tuple(map(int, revno_str.split('.')))
                    yield r_id, depth, revno, eom
            else:
                for info in merged_res:
                    if not found_start and info[0] == start_db_id:
                        found_start = True
                    if found_start:
                        if stop_db_id is not None and info[0] == stop_db_id:
                            return
                        db_id, r_id, depth, revno_str, eom = info
                        revno = tuple(map(int, revno_str.split('.')))
                        yield r_id, depth, revno, eom
            tip_db_id = next_db_id

    def iter_merge_sorted_revisions(self, start_revision_id=None,
                                    stop_revision_id=None):
        """See Branch.iter_merge_sorted_revisions()

        Note that start and stop differ from the Branch implementation, because
        stop is *always* exclusive. You can simulate the rest by careful
        selection of stop.
        """
        self.ensure_branch_tip()
        t = time.time()
        tip_db_id = self._branch_tip_db_id
        if tip_db_id is None:
            return []
        if start_revision_id is not None:
            start_db_id = self._get_db_id(start_revision_id)
        else:
            start_db_id = tip_db_id
        stop_db_id = None
        if stop_revision_id is not None:
            stop_db_id = self._get_db_id(stop_revision_id)
        # Seek fast until we find start_db_id
        merge_sorted = []
        revnos = {}
        tip_db_id = self._find_tip_containing(tip_db_id, start_db_id)
        # Now that you have found the first tip containing the given start
        # revision, pull in data until you walk off the history, or you find
        # the stop revision
        merge_sorted = list(
            self._get_merge_sorted_range(tip_db_id, start_db_id, stop_db_id))
        self._stats['query_time'] += (time.time() - t)
        return merge_sorted

    def heads(self, revision_ids):
        """Compute Graph.heads() on the given data."""
        raise NotImplementedError(self.heads)