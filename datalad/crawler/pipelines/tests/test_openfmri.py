# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##

from os.path import join as opj

from ...nodes.crawl_url import crawl_url
from ...nodes.matches import *
from ...pipeline import run_pipeline, FinishPipeline

from ...nodes.misc import Sink, assign, range_node, interrupt_if
from ...nodes.annex import Annexificator, initiate_handle
from ...pipeline import load_pipeline_from_script

from ....support.stats import ActivityStats
from ....support.annexrepo import AnnexRepo

from ....utils import chpwd
from ....tests.utils import with_tree
from ....tests.utils import SkipTest
from ....tests.utils import eq_, assert_not_equal, ok_, assert_raises
from ....tests.utils import assert_in
from ....tests.utils import skip_if_no_module
from ....tests.utils import with_tempfile
from ....tests.utils import serve_path_via_http
from ....tests.utils import skip_if_no_network
from ....tests.utils import use_cassette

from ..openfmri import pipeline as ofpipeline

from logging import getLogger
lgr = getLogger('datalad.crawl.tests')


@skip_if_no_network
@use_cassette('fixtures/vcr_cassettes/openfmri.yaml')
def __test_basic_openfmri_top_pipeline():
    skip_if_no_module('scrapy')  # e.g. not present under Python3
    sink1 = Sink()
    sink2 = Sink()
    sink_licenses = Sink()
    pipeline = [
        crawl_url("https://openfmri.org/data-sets"),
        a_href_match(".*/dataset/(?P<dataset_dir>ds0*(?P<dataset>[1-9][0-9]*))$"),
        # if we wanted we could instruct to crawl inside
        [
            crawl_url(),
            [# and collect all URLs under "AWS Link"
                css_match('.field-name-field-aws-link a',
                           xpaths={'url': '@href',
                                   'url_text': 'text()'}),
                sink2
             ],
            [# and license information
                css_match('.field-name-field-license a',
                           xpaths={'url': '@href',
                                   'url_text': 'text()'}),
                sink_licenses
            ],
        ],
        sink1
    ]

    run_pipeline(pipeline)
    # we should have collected all the URLs to the datasets
    urls = [e['url'] for e in sink1.data]
    ok_(len(urls) > 20)  # there should be at least 20 listed there
    ok_(all([url.startswith('https://openfmri.org/dataset/ds00') for url in urls]))
    # got our dataset_dir entries as well
    ok_(all([e['dataset_dir'].startswith('ds0') for e in sink1.data]))

    # and sink2 should collect everything downloadable from under AWS Link section
    # test that we got all needed tags etc propagated properly!
    all_aws_entries = sink2.get_values('dataset', 'url_text', 'url')
    ok_(len(all_aws_entries) > len(urls))  # that we have at least as many ;-)
    #print('\n'.join(map(str, all_aws_entries)))
    all_licenses = sink_licenses.get_values('dataset', 'url_text', 'url')
    eq_(len(all_licenses), len(urls))
    #print('\n'.join(map(str, all_licenses)))


@skip_if_no_network
@use_cassette('fixtures/vcr_cassettes/openfmri-1.yaml')
@with_tempfile(mkdir=True)
def __test_basic_openfmri_dataset_pipeline_with_annex(path):
    skip_if_no_module('scrapy')  # e.g. not present under Python3
    dataset_index = 1
    dataset_name = 'ds%06d' % dataset_index
    dataset_url = 'https://openfmri.org/dataset/' + dataset_name
    # needs to be a non-existing directory
    handle_path = opj(path, dataset_name)
    # we need to pre-initiate handle
    list(initiate_handle('openfmri', dataset_index, path=handle_path)())

    annex = Annexificator(
        handle_path,
        create=False,  # must be already initialized etc
        options=["-c", "annex.largefiles=exclude=*.txt and exclude=README"])

    pipeline = [
        crawl_url(dataset_url),
        [  # changelog
               a_href_match(".*release_history.txt"),  # , limit=1
               assign({'filename': 'changelog.txt'}),
               annex,
        ],
        [  # and collect all URLs under "AWS Link"
            css_match('.field-name-field-aws-link a',
                      xpaths={'url': '@href',
                              'url_text': 'text()'}),
            # TODO:  here we need to provide means to rename some files
            # but first those names need to be extracted... pretty much
            # we need conditional sub-pipelines which do yield (or return?)
            # some result back to the main flow, e.g.
            # get_url_filename,
            # [ {'yield_result': True; },
            #   field_matches_re(filename='.*release_history.*'),
            #   assign({'filename': 'license:txt'}) ]
            annex,
        ],
        [  # and license information
            css_match('.field-name-field-license a',
                      xpaths={'url': '@href',
                              'url_text': 'text()'}),
            assign({'filename': 'license.txt'}),
            annex,
        ],
    ]

    run_pipeline(pipeline)


@with_tree(tree={
    'ds666': {
        'index.html': """<html><body>
                            <a href="release_history.txt">Release History</a>
                            <a href="ds666_R1.0.0.tar.gz">Raw data on AWS version 1</a>
                            <a href="ds666_R1.0.1.tar.gz">Raw data on AWS version 2</a>
                          </body></html>""",
        'release_history.txt': '1.0.1 fixed\n1.0.0 whatever',
        'ds666_R1.0.0.tar.gz': {'ds666': {'sub-1': {'anat': {'sub-1_T1w.dat': "mighty load 1.0.0"}}}},
        'ds666_R1.0.1.tar.gz': {'ds666': {'sub-1': {'anat': {'sub-1_T1w.dat': "mighty load 1.0.1"}}}},
    }
})
@serve_path_via_http
@with_tempfile(mkdir=True)
def test_openfmri_pipeline1(ind, topurl, outd):
    repo = AnnexRepo(outd, create=True)
    with chpwd(outd):
        pipeline = ofpipeline('ds666', versioned_urls=False, topurl=topurl)
        out = run_pipeline(pipeline)
    # Inspect the tree -- that we have all the branches
    eq_(set(repo.git_get_branches()), {'master', 'incoming', 'incoming-processed', 'git-annex'})
    # We do not have custom changes in master yet, so it just follows incoming-processed atm
    eq_(repo.git_get_hexsha('master'), repo.git_get_hexsha('incoming-processed'))
    # but that one is different from incoming
    assert_not_equal(repo.git_get_hexsha('incoming'), repo.git_get_hexsha('incoming-processed'))

    # TODO: tags for the versions
    # actually the tree should look quite neat with 1.0.0 tag having 1 parent in incoming
    # 1.0.1 having 1.0.0 and the 2nd commit in incoming as parents

    # TODO: fix up commit messages in incoming
    eq_(len(out), 1)
    print outd
    raise SkipTest("many TODO")