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

from bzrlib import trace

from bzrlib.plugin.history_db import schema


def import_from_branch(a_branch, db=None):
    """Import the history data from a_branch into the database."""
    db_conn = dbapi2.connect(db)
    if not schema.is_initialized(db_conn):
        trace.note('Initialized database: %s' % (db,))
        schema.create_sqlite_db(db_conn)
    tip_key = (a_branch.last_revision(),)
    kg = a_branch.repository.revisions.get_known_graph_ancestry([tip_key])
    merge_sorted = kg.merge_sort(tip_key)
    cur_tip = None
    new_nodes = []
    cursor = db_conn.cursor()
    cursor.execute("BEGIN")
    for node in reversed(merge_sorted):
        db_id = schema.ensure_revision(node.key[0])
        new_nodes.append((db_id, node))
        if node.merge_depth == 0:
            # We have a new tip revision, store the current merged nodes
            for merged_node_id, new_node in new_nodes:
                schema.create_dotted_revno(
                    tip_revision=db_id,
                    merged_revision=merged_node_id,
                    revno=''.join(map(str, node.revno)),
                    end_of_merge=node.end_of_merge,
                    merge_depth=node.merge_depth
                    )
            new_nodes = []
    if new_nodes:
        raise ValueError('Somehow we didn\'t end up at a mainline revision.')
    cursor.execute('COMMIT')
