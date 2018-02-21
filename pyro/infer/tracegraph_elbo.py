from __future__ import absolute_import, division, print_function

import warnings

from operator import itemgetter
import networkx
import numpy as np
import torch

import pyro
import pyro.poutine as poutine
from pyro.distributions.util import is_identically_zero
from pyro.infer.util import torch_backward, torch_data_sum
from pyro.distributions.util import sum_leftmost, sum_rightmost, sum_rightmost_keep, sum_leftmost_keep
from pyro.poutine.util import prune_subsample_sites
from pyro.util import check_model_guide_match, detach_iterable, ng_zeros


def _get_baseline_options(site):
    """
    Extracts baseline options from ``site["infer"]["baseline"]``.
    """
    # XXX default for baseline_beta currently set here
    options_dict = site["infer"].get("baseline", {}).copy()
    options_tuple = (options_dict.pop('nn_baseline', None),
                     options_dict.pop('nn_baseline_input', None),
                     options_dict.pop('use_decaying_avg_baseline', False),
                     options_dict.pop('baseline_beta', 0.90),
                     options_dict.pop('baseline_value', None))
    if options_dict:
        raise ValueError("Unrecognized baseline options: {}".format(options_dict.keys()))
    return options_tuple


def ones_on_right(x):
    n = 0
    for d in reversed(x.size()):
        if d==1:
            n += 1
        else:
            return n
    return 0


def unsqueeze_on_right(x, dim):
    for _ in range(dim):
        x = x.unsqueeze(-1)
    return x


def _compute_downstream_costs(model_trace, guide_trace,  #
                              model_vec_md_nodes, guide_vec_md_nodes,  #
                              non_reparam_nodes, include_nodes=False):
    # recursively compute downstream cost nodes for all sample sites in model and guide
    # (even though ultimately just need for non-reparameterizable sample sites)
    # 1. downstream costs used for rao-blackwellization
    # 2. model observe sites (as well as terms that arise from the model and guide having different
    # dependency structures) are taken care of via 'children_in_model' below
    topo_sort_guide_nodes = list(reversed(list(networkx.topological_sort(guide_trace))))
    topo_sort_guide_nodes = [x for x in topo_sort_guide_nodes
                             if guide_trace.nodes[x]["type"] == "sample"]
    ordered_guide_nodes_dict = dict(list(zip(topo_sort_guide_nodes, list(range(len(topo_sort_guide_nodes))))))

    downstream_guide_cost_nodes = {}
    downstream_costs = {}

    stacks = model_trace.graph["vectorized_map_data_info"]['vec_md_stacks']
    #for k in stacks:
    #    print("stack[%s]" % k, stacks[k])

    def compatible_branches(x, y):
        #compatible = True
        n_compatible = 0
        #n_min = min(len(stacks[x]), len(stacks[y]))
        #n_max = max(len(stacks[x]), len(stacks[y]))
        n_x = len(stacks[x])
        n_y = len(stacks[y])
        for xframe, yframe in zip(stacks[x], stacks[y]):
            if xframe.name == yframe.name:
            #if xframe.name != yframe.name:
                n_compatible += 1
                #compatible = False
                # break
        #empty = torch.zeros(0)
        #n_incompatible = n_max - n_compatible
        #print("xy", x, y, n_compatible, n_max, downstream_costs.get(x, empty).size(), downstream_costs.get(y, empty).size())
        return n_compatible, n_x, n_y #n_incompatible
        #return n_compatible, n_max, n_min #n_incompatible

    def get_bc_penalty(source, dest):
        source_dim = source.dim()
        bc_penalty = float(np.prod(dest.size()[-source_dim:])) / float(np.prod(source.size()))
        return bc_penalty

    def add(dest, source, dest_node, source_node):
        #if dest_node == 'c4':
        #    print("adding %s to c4" % source_node)
        #print('[%d]' % id(source), source.t().data.numpy())
        n_compatible, n_source, n_dest = compatible_branches(source_node, dest_node)
        #n_compatible, n_incompatible = compatible_branches(source_node, dest_node)
        if False:
        #if n_incompatible == 0:
            print("dest, source", dest_node, source_node, "n_comp", n_compatible, n_max, n_min) #dest.size(), source.size())
            #print("comp dest, source", dest_node, source_node, "n_comp", n_compatible) #dest.size(), source.size())
            return dest
        else:
            to_sum = model_trace.nodes[source_node]['batch_log_pdf'].dim() - n_compatible
            to_sum = n_source - n_compatible
            print("incompatible dest, source", dest_node, source_node, "n_comp", n_compatible, "tosum", to_sum)
            result1 = dest
            result2 = sum_leftmost(source, to_sum)
            #bc_penalty = float(np.prod(dest.size())) / float(np.prod(result2.size()))
            #oor = ones_on_right(model_trace.nodes[dest_node]['batch_log_pdf'])
            #result2 = sum_rightmost_keep(result2, oor)
            bc_penalty = get_bc_penalty(result2, dest)
            print("result sizes:", result1.size(), result2.size(), "to sum", to_sum, "bcpen", bc_penalty)#, "oor", oor)
            result = bc_penalty * result1 + result2
            print("result sizes:", result1.size(), result2.size(), " ==> ", result.size())
            print()
            return result

    def get_dc_size(node):
        if node in downstream_costs:
            return downstream_costs[node].size()
        else:
            return 'NoSize'

    for node in topo_sort_guide_nodes:
        downstream_costs[node] = model_trace.nodes[node]['batch_log_pdf'] - guide_trace.nodes[node]['batch_log_pdf']
        nodes_included_in_sum = set([node])
        downstream_guide_cost_nodes[node] = set([node])
        # make more efficient by ordering children appropriately (higher children first)
        children = [(k, -ordered_guide_nodes_dict[k]) for k in guide_trace.successors(node)]
        sorted_children = sorted(children, key=itemgetter(1))
        for child, _ in sorted_children:
            child_cost_nodes = downstream_guide_cost_nodes[child]
            downstream_guide_cost_nodes[node].update(child_cost_nodes)
            if nodes_included_in_sum.isdisjoint(child_cost_nodes):  # avoid duplicates
                #compatible_branches(node, child)
                #downstream_costs[node] = downstream_costs[node] + downstream_costs[child]
                print('\n[add #1] dest = %s source = %s' % (node, child), '\ndest_dc_size =   ', get_dc_size(node),
                      '\nsource_dc_size = ', get_dc_size(child), '\ndest_node_size = ', guide_trace.nodes[node]['batch_log_pdf'].size())
                downstream_costs[node] = add(downstream_costs[node], downstream_costs[child], node, child)
                downstream_costs[node] = sum_leftmost(downstream_costs[node], -guide_trace.nodes[node]['batch_log_pdf'].dim())
                nodes_included_in_sum.update(child_cost_nodes)
        missing_downstream_costs = downstream_guide_cost_nodes[node] - nodes_included_in_sum
        # include terms we missed because we had to avoid duplicates
        for missing_node in missing_downstream_costs:
            #compatible_branches(node, missing_node)
            #downstream_costs[node] = downstream_costs[node] + model_trace.nodes[missing_node]['batch_log_pdf'] - \
            #                          guide_trace.nodes[missing_node]['batch_log_pdf']
            print('[add #2] dest = %s source = %s\n' % (node, missing_node), 'dest_dc_size = ', get_dc_size(node),
                  '\nsource_dc_size = ', get_dc_size(missing_node), '\ndest_node_size', guide_trace.nodes[node]['batch_log_pdf'].size())
            downstream_costs[node] = add(downstream_costs[node],
                                         model_trace.nodes[missing_node]['batch_log_pdf'] - \
                                         guide_trace.nodes[missing_node]['batch_log_pdf'],
                                         node, missing_node)

    def sumout():
        # sum out any parts of downstream costs that need summing out and make
        # sure that downstream costs have the right size
        for k in downstream_costs:
            #print(">", k, "dc", downstream_costs[k].size(), "blp", guide_trace.nodes[k]['batch_log_pdf'].size())
            #downstream_costs[k] = sum_leftmost(downstream_costs[k], -guide_trace.nodes[k]['batch_log_pdf'].dim())
            #print("> >", k, "dc_sumlefmost", downstream_costs[k].size())
            oor = ones_on_right(guide_trace.nodes[k]['batch_log_pdf'])
            presize = downstream_costs[k].size()
            downstream_costs[k] = sum_rightmost_keep(downstream_costs[k], oor)
            print("sumout node %s" % k, presize, "===>", downstream_costs[k].size())
            #downstream_costs[k] = unsqueeze_on_right(downstream_costs[k], oor)
            #print("> > >", k, "dc_sr", downstream_costs[k].size())

    #sumout()

    # finish assembling complete downstream costs
    # (the above computation may be missing terms from model)
    # XXX can we cache some of the sums over children_in_model to make things more efficient?
    for site in topo_sort_guide_nodes:
    #for site in non_reparam_nodes:
        children_in_model = set()
        for node in downstream_guide_cost_nodes[site]:
            children_in_model.update(model_trace.successors(node))
        # remove terms accounted for above
        children_in_model.difference_update(downstream_guide_cost_nodes[site])
        for child in children_in_model:
            assert (model_trace.nodes[child]["type"] == "sample")
            print('[add #3] dest = %s source = %s\n' % (site, child), 'dest_dc_size = ', get_dc_size(site),
                  'source_dc_size = ', get_dc_size(child), 'dest_node_size', guide_trace.nodes[site]['batch_log_pdf'].size())
            downstream_costs[site] = add(downstream_costs[site], model_trace.nodes[child]['batch_log_pdf'], site, child)
            #downstream_costs[site] = downstream_costs[site] + model_trace.nodes[child]['batch_log_pdf']
            #compatible_branches(site, child)
            downstream_guide_cost_nodes[site].update([child])

    sumout()

    if include_nodes:
        return downstream_costs, downstream_guide_cost_nodes
    return downstream_costs


def _compute_elbo_reparam(model_trace, guide_trace, non_reparam_nodes):
    elbo = 0.0
    surrogate_elbo = 0.0
    for name, model_site in model_trace.nodes.items():
        if model_site["type"] == "sample":
            if model_site["is_observed"]:
                elbo += model_site["log_pdf"]
                surrogate_elbo += model_site["log_pdf"]
            else:
                # deal with log p(z|...) term
                elbo += model_site["log_pdf"]
                surrogate_elbo += model_site["log_pdf"]
                # deal with log q(z|...) term, if present
                guide_site = guide_trace.nodes[name]
                elbo -= guide_site["log_pdf"]
                entropy_term = guide_site["score_parts"].entropy_term
                if not is_identically_zero(entropy_term):
                    surrogate_elbo -= entropy_term.sum()

    # elbo is never differentiated, surragate_elbo is

    return torch_data_sum(elbo), surrogate_elbo


def _compute_elbo_non_reparam(guide_trace, guide_vec_md_nodes,  #
                              non_reparam_nodes, downstream_costs):
    # construct all the reinforce-like terms.
    # we include only downstream costs to reduce variance
    # optionally include baselines to further reduce variance
    # XXX should the average baseline be in the param store as below?
    surrogate_elbo = 0.0
    baseline_loss = 0.0
    for node in non_reparam_nodes:
        guide_site = guide_trace.nodes[node]
        log_pdf_key = 'batch_log_pdf' if node in guide_vec_md_nodes else 'log_pdf'
        downstream_cost = downstream_costs[node]
        baseline = 0.0
        (nn_baseline, nn_baseline_input, use_decaying_avg_baseline, baseline_beta,
            baseline_value) = _get_baseline_options(guide_site)
        use_nn_baseline = nn_baseline is not None
        use_baseline_value = baseline_value is not None
        assert(not (use_nn_baseline and use_baseline_value)), \
            "cannot use baseline_value and nn_baseline simultaneously"
        if use_decaying_avg_baseline:
            avg_downstream_cost_old = pyro.param("__baseline_avg_downstream_cost_" + node,
                                                 ng_zeros(1), tags="__tracegraph_elbo_internal_tag")
            avg_downstream_cost_new = (1 - baseline_beta) * downstream_cost + \
                baseline_beta * avg_downstream_cost_old
            avg_downstream_cost_old.data = avg_downstream_cost_new.data  # XXX copy_() ?
            baseline += avg_downstream_cost_old
        if use_nn_baseline:
            # block nn_baseline_input gradients except in baseline loss
            baseline += nn_baseline(detach_iterable(nn_baseline_input))
        elif use_baseline_value:
            # it's on the user to make sure baseline_value tape only points to baseline params
            baseline += baseline_value
        if use_nn_baseline or use_baseline_value:
            # accumulate baseline loss
            baseline_loss += torch.pow(downstream_cost.detach() - baseline, 2.0).sum()

        score_function_term = guide_site["score_parts"].score_function
        if log_pdf_key == 'log_pdf':
            score_function_term = score_function_term.sum()
        if use_nn_baseline or use_decaying_avg_baseline or use_baseline_value:
            if downstream_cost.size() != baseline.size():
                raise ValueError("Expected baseline at site {} to be {} instead got {}".format(
                    node, downstream_cost.size(), baseline.size()))
            downstream_cost = downstream_cost - baseline
        surrogate_elbo += (score_function_term * downstream_cost.detach()).sum()

    return surrogate_elbo, baseline_loss


class TraceGraph_ELBO(object):
    """
    A TraceGraph implementation of ELBO-based SVI. The gradient estimator
    is constructed along the lines of reference [1] specialized to the case
    of the ELBO. It supports arbitrary dependency structure for the model
    and guide as well as baselines for non-reparameteriable random variables.
    Where possible, dependency information as recorded in the TraceGraph is
    used to reduce the variance of the gradient estimator.

    References

    [1] `Gradient Estimation Using Stochastic Computation Graphs`,
        John Schulman, Nicolas Heess, Theophane Weber, Pieter Abbeel

    [2] `Neural Variational Inference and Learning in Belief Networks`
        Andriy Mnih, Karol Gregor
    """
    def __init__(self, num_particles=1, enum_discrete=False):
        """
        :param num_particles: the number of particles (samples) used to form the estimator
        :param bool enum_discrete: whether to sum over discrete latent variables, rather than sample them
        """
        super(TraceGraph_ELBO, self).__init__()
        self.num_particles = num_particles
        self.enum_discrete = enum_discrete

    def _get_traces(self, model, guide, *args, **kwargs):
        """
        runs the guide and runs the model against the guide with
        the result packaged as a tracegraph generator
        """

        for i in range(self.num_particles):
            if self.enum_discrete:
                raise NotImplementedError("https://github.com/uber/pyro/issues/220")

            guide_trace = poutine.trace(guide,
                                        graph_type="dense").get_trace(*args, **kwargs)
            model_trace = poutine.trace(poutine.replay(model, guide_trace),
                                        graph_type="dense").get_trace(*args, **kwargs)

            check_model_guide_match(model_trace, guide_trace)
            guide_trace = prune_subsample_sites(guide_trace)
            model_trace = prune_subsample_sites(model_trace)

            weight = 1.0 / self.num_particles
            yield weight, model_trace, guide_trace

    def loss(self, model, guide, *args, **kwargs):
        """
        :returns: returns an estimate of the ELBO
        :rtype: float

        Evaluates the ELBO with an estimator that uses num_particles many samples/particles.
        """
        elbo = 0.0
        for weight, model_trace, guide_trace in self._get_traces(model, guide, *args, **kwargs):
            guide_trace.log_pdf(), model_trace.log_pdf()

            elbo_particle = 0.0

            for name in model_trace.nodes.keys():
                if model_trace.nodes[name]["type"] == "sample":
                    if model_trace.nodes[name]["is_observed"]:
                        elbo_particle += model_trace.nodes[name]["log_pdf"]
                    else:
                        elbo_particle += model_trace.nodes[name]["log_pdf"]
                        elbo_particle -= guide_trace.nodes[name]["log_pdf"]

            elbo += torch_data_sum(weight * elbo_particle)

        loss = -elbo
        if np.isnan(loss):
            warnings.warn('Encountered NAN loss')
        return loss

    def loss_and_grads(self, model, guide, *args, **kwargs):
        """
        :returns: returns an estimate of the ELBO
        :rtype: float

        Computes the ELBO as well as the surrogate ELBO that is used to form the gradient estimator.
        Performs backward on the latter. Num_particle many samples are used to form the estimators.
        If baselines are present, a baseline loss is also constructed and differentiated.
        """
        loss = 0.0
        for weight, model_trace, guide_trace in self._get_traces(model, guide, *args, **kwargs):
            loss += self._loss_and_grads_particle(weight, model_trace, guide_trace)
        return loss

    def _loss_and_grads_particle(self, weight, model_trace, guide_trace):
        # get info regarding rao-blackwellization of vectorized map_data
        guide_vec_md_info = guide_trace.graph["vectorized_map_data_info"]
        model_vec_md_info = model_trace.graph["vectorized_map_data_info"]
        guide_vec_md_condition = guide_vec_md_info['rao-blackwellization-condition']
        model_vec_md_condition = model_vec_md_info['rao-blackwellization-condition']
        do_vec_rb = guide_vec_md_condition and model_vec_md_condition
        if not do_vec_rb:
            warnings.warn(
                "Unable to do fully-vectorized Rao-Blackwellization in TraceGraph_ELBO. "
                "Falling back to higher-variance gradient estimator. "
                "Try to avoid these issues in your model and guide:\n{}".format("\n".join(
                    guide_vec_md_info["warnings"] | model_vec_md_info["warnings"])))
        guide_vec_md_nodes = guide_vec_md_info['nodes'] if do_vec_rb else set()
        model_vec_md_nodes = model_vec_md_info['nodes'] if do_vec_rb else set()

        # have the trace compute all the individual (batch) log pdf terms
        # and score function terms (if present) so that they are available below
        model_trace.compute_batch_log_pdf()
        guide_trace.compute_score_parts()

        # compute elbo for reparameterized nodes
        non_reparam_nodes = set(guide_trace.nonreparam_stochastic_nodes)
        elbo, surrogate_elbo = _compute_elbo_reparam(model_trace, guide_trace, non_reparam_nodes)

        # the following computations are only necessary if we have non-reparameterizable nodes
        baseline_loss = 0.0
        if non_reparam_nodes:
            downstream_costs = _compute_downstream_costs(
                    model_trace, guide_trace,  model_vec_md_nodes, guide_vec_md_nodes, non_reparam_nodes)
            surrogate_elbo_term, baseline_loss = _compute_elbo_non_reparam(
                    guide_trace, guide_vec_md_nodes, non_reparam_nodes, downstream_costs)
            surrogate_elbo += surrogate_elbo_term

        # collect parameters to train from model and guide
        trainable_params = set(site["value"]
                               for trace in (model_trace, guide_trace)
                               for site in trace.nodes.values()
                               if site["type"] == "param")

        if trainable_params:
            surrogate_loss = -surrogate_elbo
            torch_backward(weight * (surrogate_loss + baseline_loss))
            pyro.get_param_store().mark_params_active(trainable_params)

        loss = -elbo
        if np.isnan(loss):
            warnings.warn('Encountered NAN loss')
        return weight * loss
