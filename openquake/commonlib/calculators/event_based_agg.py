#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2015, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import logging
import operator

import numpy

from openquake.baselib.general import AccumDict, humansize
from openquake.commonlib.calculators import base
from openquake.commonlib import readinput, parallel, datastore
from openquake.risklib import riskinput
from openquake.commonlib.parallel import apply_reduce

NUM_OUTPUTS = 2  # event loss table and insured loss table

elt_dt = numpy.dtype([('rup_id', numpy.uint32), ('loss', numpy.float32)])


def cube(O, L, R, factory):
    """
    :param O: the number of different outputs
    :param L: the number of loss types
    :param R: the number of realizations
    :param factory: factory used to initialize the elements (int or list)
    :returns: a numpy array of shape (O, L, R)
    """
    losses = numpy.zeros((O, L, R), object)
    for o in range(O):
        for l in range(L):
            for r in range(R):
                losses[o, l, r] = factory()
    return losses


@parallel.litetask
def ebr(riskinputs, riskmodel, rlzs_assoc, monitor):
    """
    :param riskinputs:
        a list of :class:`openquake.risklib.riskinput.RiskInput` objects
    :param riskmodel:
        a :class:`openquake.risklib.riskinput.RiskModel` instance
    :param rlzs_assoc:
        a class:`openquake.commonlib.source.RlzsAssoc` instance
    :param monitor:
        :class:`openquake.commonlib.parallel.PerformanceMonitor` instance
    :returns:
        a dictionary rlz.ordinal -> (loss_type, tag) -> AccumDict()
    """
    lt_idx = {lt: i for i, lt in enumerate(riskmodel.get_loss_types())}
    losses = cube(
        NUM_OUTPUTS, len(lt_idx), len(rlzs_assoc.realizations), list)
    for out_by_rlz in riskmodel.gen_outputs(riskinputs, rlzs_assoc, monitor):
        rup_slice = out_by_rlz.rup_slice
        rup_ids = range(rup_slice.start, rup_slice.stop)
        for rlz, out in zip(rlzs_assoc.realizations, out_by_rlz):
            lt = lt_idx[out.loss_type]
            agg_losses = out.event_loss_per_asset.sum(axis=1)
            agg_ins_losses = out.insured_loss_per_asset.sum(axis=1)
            for rup_id, loss, ins_loss in zip(
                    rup_ids, agg_losses, agg_ins_losses):
                if loss > 0:
                    losses[0, lt, rlz.ordinal].append(
                        numpy.array([(rup_id, loss)], elt_dt))
                if ins_loss > 0:
                    losses[1, lt, rlz.ordinal].append(
                        numpy.array([(rup_id, ins_loss)], elt_dt))
    for idx, arrays in numpy.ndenumerate(losses):
        if arrays:
            losses[idx] = [numpy.concatenate(arrays)]
    return losses


@base.calculators.add('ebr')
class EventBasedRiskCalculator(base.RiskCalculator):
    """
    Event based PSHA calculator generating the ruptures only
    """
    pre_calculator = 'event_based_rupture'
    core_func = ebr

    epsilon_matrix = datastore.persistent_attribute('/epsilon_matrix')
    event_loss_table = datastore.persistent_attribute(
        '/event_loss_table-rlzs')
    insured_loss_table = datastore.persistent_attribute(
        '/insured_loss_table-rlzs')
    is_stochastic = True

    def pre_execute(self):
        """
        Read the precomputed ruptures (or compute them on the fly) and
        prepare some empty files in the export directory to store the gmfs
        (if any). If there were pre-existing files, they will be erased.
        """
        super(EventBasedRiskCalculator, self).pre_execute()

        oq = self.oqparam
        epsilon_sampling = oq.epsilon_sampling
        correl_model = readinput.get_correl_model(oq)
        gsims_by_col = self.rlzs_assoc.get_gsims_by_col()
        assets_by_site = self.assets_by_site

        logging.info('Populating the risk inputs')
        rup_by_tag = sum(self.datastore['sescollection'], AccumDict())
        all_ruptures = [rup_by_tag[tag] for tag in sorted(rup_by_tag)]
        num_samples = min(len(all_ruptures), epsilon_sampling)
        eps_dict = riskinput.make_eps_dict(
            assets_by_site, num_samples, oq.master_seed, oq.asset_correlation)
        logging.info('Generated %d epsilons', num_samples * len(eps_dict))
        self.epsilon_matrix = numpy.array(
            [eps_dict[a['asset_ref']] for a in self.assetcol])
        self.riskinputs = list(self.riskmodel.build_inputs_from_ruptures(
            self.sitecol.complete, all_ruptures, gsims_by_col,
            oq.truncation_level, correl_model, eps_dict,
            oq.concurrent_tasks or 1))
        logging.info('Built %d risk inputs', len(self.riskinputs))

        loss_types = self.riskmodel.get_loss_types()
        self.L = len(loss_types)
        self.R = len(self.rlzs_assoc.realizations)
        for loss_type in loss_types:
            for rlz in self.rlzs_assoc.realizations:
                key = '/%s/%s' % (loss_type, rlz.uid)
                self.datastore.hdf5.create_dataset(
                    '/event_loss_table-rlzs' + key, (0,), elt_dt,
                    chunks=True, maxshape=(None,))
                if oq.insured_losses:
                    self.datastore.hdf5.create_dataset(
                        '/insured_loss_table-rlzs' + key, (0,), elt_dt,
                        chunks=True, maxshape=(None,))

    def execute(self):
        self.monitor.oqparam = oq = self.oqparam
        if self.pre_calculator == 'event_based_rupture':
            self.monitor.assets_by_site = self.assets_by_site
            self.monitor.num_assets = self.count_assets()
        return apply_reduce(
            self.core_func.__func__,
            (self.riskinputs, self.riskmodel, self.rlzs_assoc, self.monitor),
            concurrent_tasks=oq.concurrent_tasks,
            agg=self.agg, acc=cube(NUM_OUTPUTS, self.L, self.R, list),
            weight=operator.attrgetter('weight'),
            key=operator.attrgetter('col_id'))

    def agg(self, acc, losses):
        for idx, arrays in numpy.ndenumerate(losses):
            acc[idx].extend(arrays)
        return acc

    def post_execute(self, result):
        acc = cube(NUM_OUTPUTS, self.L, self.R, int)
        saved_mb = 0
        rlzs = self.rlzs_assoc.realizations
        loss_types = self.riskmodel.get_loss_types()
        with self.monitor('saving loss table',
                          autoflush=True, measuremem=True):
            for (i, l, r), arrays in numpy.ndenumerate(result):
                if not arrays:  # empty list
                    continue
                lt = loss_types[l]
                uid = rlzs[r].uid
                n = acc[i, l, r]
                if i == 0:
                    elt = self.event_loss_table['%s/%s' % (lt, uid)]
                elif self.oqparam.insured_losses:
                    elt = self.insured_loss_table['%s/%s' % (lt, uid)]
                else:
                    continue
                losses = numpy.concatenate(arrays)
                n1 = n + len(losses)
                elt.resize((n1,))
                saved_mb += losses.nbytes
                elt[n:n1] = losses
                acc[i, l, r] = n1
            self.datastore.hdf5.flush()
            if saved_mb > 1:
                logging.info('Saved %s of data', humansize(saved_mb))
        return {}
