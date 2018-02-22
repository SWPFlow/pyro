from __future__ import absolute_import, division, print_function

import numpy as np

import pytest
import torch
from torch.autograd import Variable

import pyro
import pyro.poutine as poutine
import pyro.distributions as dist
from pyro.infer.tracegraph_elbo import _compute_downstream_costs
from pyro.poutine.util import prune_subsample_sites
from tests.common import assert_equal


def big_model_guide(include_obs=True, include_single=False, include_inner_1=False, flip_c23=False):
    p0 = Variable(torch.Tensor([np.exp(-0.25)]), requires_grad=True)
    p1 = Variable(torch.Tensor([np.exp(-0.35)]), requires_grad=True)
    p2 = Variable(torch.Tensor([np.exp(-0.70)]), requires_grad=True)
    pyro.sample("a1", dist.Bernoulli(p0))
    if include_single:
        with pyro.iarange("iarange_single", 5, 5) as ind_single:
            pyro.sample("b0", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_single)]))
    with pyro.iarange("iarange_outer", 2, 2) as ind_outer:
        pyro.sample("b1", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_outer)]))
        if include_inner_1:
            with pyro.iarange("iarange_inner_1", 3, 3) as ind_inner:
                pyro.sample("c1", dist.Bernoulli(p1).reshape(sample_shape=[len(ind_inner), 1]))
                if flip_c23 and not include_obs:
                    pyro.sample("c3", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_inner), 1]))
                    pyro.sample("c2", dist.Bernoulli(p1).reshape(sample_shape=[len(ind_inner), len(ind_outer)]))
                else:
                    pyro.sample("c2", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_inner), len(ind_outer)]))
                    pyro.sample("c3", dist.Bernoulli(p2).reshape(sample_shape=[len(ind_inner), 1]))
        with pyro.iarange("iarange_inner_2", 4, 4) as ind_inner:
            pyro.sample("d1", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_inner), 1]))
            d2 = pyro.sample("d2", dist.Bernoulli(p2).reshape(sample_shape=[len(ind_inner), len(ind_outer)]))
            if include_obs:
                pyro.sample("obs", dist.Bernoulli(p0).reshape(sample_shape=[len(ind_inner), len(ind_outer)]),
                            obs=Variable(torch.ones(d2.size())))


@pytest.mark.parametrize("include_inner_1", [True, False])
@pytest.mark.parametrize("include_single", [True, False])
@pytest.mark.parametrize("flip_c23", [True, False])
def test_compute_downstream_costs_big_model_guide_pair(include_inner_1, include_single, flip_c23):
    guide_trace = poutine.trace(big_model_guide,
                                graph_type="dense").get_trace(include_obs=False, include_inner_1=include_inner_1,
                                                              include_single=include_single, flip_c23=flip_c23)
    model_trace = poutine.trace(poutine.replay(big_model_guide, guide_trace),
                                graph_type="dense").get_trace(include_obs=True, include_inner_1=include_inner_1,
                                                              include_single=include_single, flip_c23=flip_c23)

    guide_trace = prune_subsample_sites(guide_trace)
    model_trace = prune_subsample_sites(model_trace)
    model_trace.compute_batch_log_pdf()
    guide_trace.compute_batch_log_pdf()

    guide_vec_md_info = guide_trace.graph["vectorized_map_data_info"]
    model_vec_md_info = model_trace.graph["vectorized_map_data_info"]
    guide_vec_md_condition = guide_vec_md_info['rao-blackwellization-condition']
    model_vec_md_condition = model_vec_md_info['rao-blackwellization-condition']
    do_vec_rb = guide_vec_md_condition and model_vec_md_condition
    guide_vec_md_nodes = guide_vec_md_info['nodes'] if do_vec_rb else set()
    model_vec_md_nodes = model_vec_md_info['nodes'] if do_vec_rb else set()
    non_reparam_nodes = set(guide_trace.nonreparam_stochastic_nodes)

    dc, dc_nodes = _compute_downstream_costs(model_trace, guide_trace,
                                             model_vec_md_nodes, guide_vec_md_nodes,
                                             non_reparam_nodes, include_nodes=True)

    expected_nodes_full_model = {'a1': {'c2', 'a1', 'd1', 'c1', 'obs', 'b1', 'd2', 'c3', 'b0'}, 'd2': {'obs', 'd2'},
                                 'd1': {'obs', 'd1', 'd2'}, 'c3': {'d2', 'obs', 'd1', 'c3'},
                                 'b0': {'b0', 'd1', 'c1', 'obs', 'b1', 'd2', 'c3', 'c2'},
                                 'b1': {'obs', 'b1', 'd1', 'd2', 'c3', 'c1', 'c2'},
                                 'c1': {'d1', 'c1', 'obs', 'd2', 'c3', 'c2'},
                                 'c2': {'obs', 'd1', 'c3', 'd2', 'c2'}}
    if include_inner_1 and include_single and not flip_c23:
        assert(dc_nodes == expected_nodes_full_model)

    expected_b1 = (model_trace.nodes['b1']['batch_log_pdf'] - guide_trace.nodes['b1']['batch_log_pdf'])
    expected_b1 += (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf']).sum(0)
    expected_b1 += (model_trace.nodes['d1']['batch_log_pdf'] - guide_trace.nodes['d1']['batch_log_pdf']).sum(0)
    expected_b1 += model_trace.nodes['obs']['batch_log_pdf'].sum(0, keepdim=False)
    if include_inner_1:
        expected_b1 += (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf']).sum(0)
        expected_b1 += (model_trace.nodes['c2']['batch_log_pdf'] - guide_trace.nodes['c2']['batch_log_pdf']).sum(0)
        expected_b1 += (model_trace.nodes['c3']['batch_log_pdf'] - guide_trace.nodes['c3']['batch_log_pdf']).sum(0)

    if include_single:
        expected_b0 = (model_trace.nodes['b0']['batch_log_pdf'] - guide_trace.nodes['b0']['batch_log_pdf'])
        expected_b0 += (model_trace.nodes['b1']['batch_log_pdf'] - guide_trace.nodes['b1']['batch_log_pdf']).sum()
        expected_b0 += (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf']).sum()
        expected_b0 += (model_trace.nodes['d1']['batch_log_pdf'] - guide_trace.nodes['d1']['batch_log_pdf']).sum()
        expected_b0 += model_trace.nodes['obs']['batch_log_pdf'].sum()
        if include_inner_1:
            expected_b0 += (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf']).sum()
            expected_b0 += (model_trace.nodes['c2']['batch_log_pdf'] - guide_trace.nodes['c2']['batch_log_pdf']).sum()
            expected_b0 += (model_trace.nodes['c3']['batch_log_pdf'] - guide_trace.nodes['c3']['batch_log_pdf']).sum()

    if include_inner_1:
        expected_c3 = (model_trace.nodes['c3']['batch_log_pdf'] - guide_trace.nodes['c3']['batch_log_pdf'])
        expected_c3 += (model_trace.nodes['d1']['batch_log_pdf'] - guide_trace.nodes['d1']['batch_log_pdf']).sum()
        expected_c3 += (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf']).sum()
        expected_c3 += model_trace.nodes['obs']['batch_log_pdf'].sum()

        expected_c2 = (model_trace.nodes['c2']['batch_log_pdf'] - guide_trace.nodes['c2']['batch_log_pdf'])
        expected_c2 += (model_trace.nodes['d1']['batch_log_pdf'] - guide_trace.nodes['d1']['batch_log_pdf']).sum(0)
        expected_c2 += (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf']).sum(0)
        expected_c2 += model_trace.nodes['obs']['batch_log_pdf'].sum(0, keepdim=False)

        expected_c1 = (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf'])

        if flip_c23:
            term = (model_trace.nodes['c2']['batch_log_pdf'] - guide_trace.nodes['c2']['batch_log_pdf'])
            expected_c3 += term.sum(1, keepdim=True)
            expected_c2 += model_trace.nodes['c3']['batch_log_pdf']
        else:
            expected_c2 += (model_trace.nodes['c3']['batch_log_pdf'] - guide_trace.nodes['c3']['batch_log_pdf'])
            term = (model_trace.nodes['c2']['batch_log_pdf'] - guide_trace.nodes['c2']['batch_log_pdf'])
            expected_c1 += term.sum(1, keepdim=True)
        expected_c1 += expected_c3

    expected_d1 = model_trace.nodes['d1']['batch_log_pdf'] - guide_trace.nodes['d1']['batch_log_pdf']
    term = (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf'])
    expected_d1 += term.sum(1, keepdim=True)
    expected_d1 += model_trace.nodes['obs']['batch_log_pdf'].sum(1, keepdim=True)

    expected_d2 = (model_trace.nodes['d2']['batch_log_pdf'] - guide_trace.nodes['d2']['batch_log_pdf'])
    expected_d2 += model_trace.nodes['obs']['batch_log_pdf']

    if include_single:
        assert_equal(expected_b0, dc['b0'], prec=1.0e-6)
    if include_inner_1:
        assert_equal(expected_c1, dc['c1'], prec=1.0e-6)
        assert_equal(expected_c2, dc['c2'], prec=1.0e-6)
        assert_equal(expected_c3, dc['c3'], prec=1.0e-6)
    assert_equal(expected_d2, dc['d2'], prec=1.0e-6)
    assert_equal(expected_d1, dc['d1'], prec=1.0e-6)
    assert_equal(expected_b1, dc['b1'], prec=1.0e-6)

    for k in dc:
        assert(guide_trace.nodes[k]['batch_log_pdf'].size() == dc[k].size())


def diamond_model():
    p0 = Variable(torch.Tensor([np.exp(-0.70)]), requires_grad=True)
    p1 = Variable(torch.Tensor([np.exp(-0.15)]), requires_grad=True)
    pyro.sample("a1", dist.Bernoulli(p0))
    pyro.sample("c1", dist.Bernoulli(p1))
    for i in pyro.irange("irange", 2):
        pyro.sample("b{}".format(i), dist.Bernoulli(p0 * p1))
    pyro.sample("obs", dist.Bernoulli(p0), obs=Variable(torch.ones(1)))


def diamond_guide():
    p0 = Variable(torch.Tensor([np.exp(-0.25)]), requires_grad=True)
    p1 = Variable(torch.Tensor([np.exp(-0.55)]), requires_grad=True)
    pyro.sample("a1", dist.Bernoulli(p0))
    for i in pyro.irange("irange", 2):
        pyro.sample("b{}".format(i), dist.Bernoulli(p1))
    pyro.sample("c1", dist.Bernoulli(p0))


def test_compute_downstream_costs_duplicates():
    guide_trace = poutine.trace(diamond_guide,
                                graph_type="dense").get_trace()
    model_trace = poutine.trace(poutine.replay(diamond_model, guide_trace),
                                graph_type="dense").get_trace()

    guide_trace = prune_subsample_sites(guide_trace)
    model_trace = prune_subsample_sites(model_trace)
    model_trace.compute_batch_log_pdf()
    guide_trace.compute_batch_log_pdf()

    guide_vec_md_info = guide_trace.graph["vectorized_map_data_info"]
    model_vec_md_info = model_trace.graph["vectorized_map_data_info"]
    guide_vec_md_condition = guide_vec_md_info['rao-blackwellization-condition']
    model_vec_md_condition = model_vec_md_info['rao-blackwellization-condition']
    do_vec_rb = guide_vec_md_condition and model_vec_md_condition
    guide_vec_md_nodes = guide_vec_md_info['nodes'] if do_vec_rb else set()
    model_vec_md_nodes = model_vec_md_info['nodes'] if do_vec_rb else set()
    non_reparam_nodes = set(guide_trace.nonreparam_stochastic_nodes)

    dc, dc_nodes = _compute_downstream_costs(model_trace, guide_trace,
                                             model_vec_md_nodes, guide_vec_md_nodes,
                                             non_reparam_nodes, include_nodes=True)

    expected_a1 = (model_trace.nodes['a1']['batch_log_pdf'] - guide_trace.nodes['a1']['batch_log_pdf'])
    expected_a1 += (model_trace.nodes['b1']['batch_log_pdf'] - guide_trace.nodes['b1']['batch_log_pdf'])
    expected_a1 += (model_trace.nodes['b0']['batch_log_pdf'] - guide_trace.nodes['b0']['batch_log_pdf'])
    expected_a1 += (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf'])
    expected_a1 += model_trace.nodes['obs']['batch_log_pdf']

    expected_b1 = (model_trace.nodes['b1']['batch_log_pdf'] - guide_trace.nodes['b1']['batch_log_pdf'])
    expected_b1 += (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf'])
    expected_b1 += model_trace.nodes['b0']['batch_log_pdf']
    expected_b1 += model_trace.nodes['obs']['batch_log_pdf']

    expected_c1 = (model_trace.nodes['c1']['batch_log_pdf'] - guide_trace.nodes['c1']['batch_log_pdf'])
    expected_c1 += model_trace.nodes['b0']['batch_log_pdf']
    expected_c1 += model_trace.nodes['b1']['batch_log_pdf']
    expected_c1 += model_trace.nodes['obs']['batch_log_pdf']

    assert_equal(expected_a1, dc['a1'], prec=1.0e-6)
    assert_equal(expected_b1, dc['b1'], prec=1.0e-6)
    assert_equal(expected_c1, dc['c1'], prec=1.0e-6)

    for k in dc:
        assert(guide_trace.nodes[k]['batch_log_pdf'].size() == dc[k].size())