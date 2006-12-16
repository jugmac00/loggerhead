#
# Copyright (C) 2006  Robey Pointer <robey@lag.net>
# Copyright (C) 2006  Goffredo Baroncelli <kreijack@inwind.it>
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
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#

import turbogears

from loggerhead import util


class AtomUI (object):
    
    @turbogears.expose(template='loggerhead.templates.atom', format="xml", content_type="application/atom+xml")
    def default(self, *args):
        h = util.get_history()
        pagesize = int(util.get_config().get('pagesize', '20'))

        revlist, start_revid = h.get_navigation(None, None)
        entries = list(h.get_changelist(list(revlist)[:pagesize]))

        vals = {
            'external_url': util.get_config().get('external_url'),
            'branch_name': util.get_config().get('branch_name'),
            'changes': entries,
            'util': util,
            'history': h,
            'scan_url': '/changes',
            'updated': entries[0].date.isoformat() + 'Z',
        }
        h.flush_cache()
        return vals
