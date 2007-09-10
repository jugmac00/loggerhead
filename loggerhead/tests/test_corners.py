import os

import cherrypy
from turbogears import testutil

from loggerhead.tests.test_simple import BasicTests

class TestCornerCases(BasicTests):
    """Tests that excercise various corner cases."""

    def addFileAndCommit(self, filename, commit_msg):
        """Make a trivial commit that has 'msg' as its commit message.

        The commit adds a file called 'myfilename' containing the string
        'foo'.
        """
        f = open(os.path.join(self.bzrbranch, filename), 'w')
        try:
            f.write("foo")
        finally:
            f.close()
        self.tree.add(filename)
        self.tree.commit(message=commit_msg)


    def test_survive_over_upgrade(self):
        """Check that running 'bzr upgrade' on a branch does not break an
        instance of loggerhead that's already looked at it.
        """
        self.createBranch()

        msg = 'a very exciting commit message'
        self.addFileAndCommit('myfilename', msg)

        self.setUpLoggerhead()

        testutil.create_request('/project/branch/changes')
        assert msg in cherrypy.response.body[0]

        from bzrlib.upgrade import upgrade
        from bzrlib.bzrdir import format_registry
        upgrade(self.bzrbranch, format_registry.make_bzrdir('dirstate-tags'))

        testutil.create_request('/project/branch/changes')
        assert msg in cherrypy.response.body[0]


    def test_revision_only_changing_execute_bit(self):
        """Check that a commit that only changes the execute bit of a file
        does not break the rendering."""
        self.createBranch()

        msg = 'a very exciting commit message'
        self.addFileAndCommit('myfilename', msg)

        os.chmod(os.path.join(self.bzrbranch, 'myfilename'), 0755)

        newrevid = self.tree.commit(message='make something executable')

        self.setUpLoggerhead()

        testutil.create_request('/project/branch/revision/'+newrevid)
        assert 'executable' in cherrypy.response.body[0]


    def test_empty_commit_message(self):
        """Check that an empty commit message does not break the rendering."""
        self.createBranch()

        self.addFileAndCommit('myfilename', '')

        self.setUpLoggerhead()
        testutil.create_request('/project/branch/changes')
        # It's not much of an assertion, but we only really care about
        # "assert not crashed".
        assert 'myfilename' in cherrypy.response.body[0]


    def test_whitespace_only_commit_message(self):
        """Check that a whitespace-only commit message does not break the
        rendering."""
        self.createBranch()

        self.addFileAndCommit('myfilename', '   ')

        self.setUpLoggerhead()
        testutil.create_request('/project/branch/changes')
        # It's not much of an assertion, but we only really care about
        # "assert not crashed".
        assert 'myfilename' in cherrypy.response.body[0]
