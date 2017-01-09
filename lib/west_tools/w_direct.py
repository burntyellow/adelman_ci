# Copyright (C) 2013 Matthew C. Zwier and Lillian T. Chong
#
# This file is part of WESTPA.
#
# WESTPA is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# WESTPA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with WESTPA.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, division; __metaclass__ = type
import logging

# Let's suppress those numpy warnings.
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=RuntimeWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

import sys, random, math
import numpy, h5py
from h5py import h5s

import westpa
from west.data_manager import weight_dtype, n_iter_dtype, seg_id_dtype
from westtools import (WESTMasterCommand, WESTParallelTool, WESTDataReader, IterRangeSelection, WESTSubcommand,
                       ProgressIndicatorComponent)
from westpa import h5io
from westpa.kinetics import labeled_flux_to_rate, sequence_macro_flux_to_rate, sequence_macro_flux_to_rate_bs
from westpa.kinetics.matrates import get_macrostate_rates
# This is the base tool class.  We're going to use it for the post analysis stuff, as well.
from westpa.kintool import WESTKinAvg, AverageCommands

import mclib
from mclib import mcbs_correltime, mcbs_ci_correl_rw, _1D_simple_eval_block, _2D_simple_eval_block

# We'll need to integrate this properly.
log = logging.getLogger('westtools.w_kinavg')

from westtools.dtypes import iter_block_ci_dtype as ci_dtype

# From w_kinetics.
# Do we still need this?
from westtools.dtypes import ed_list_dtype
from westpa.binning import index_dtype
from westpa.kinetics._kinetics import _fast_transition_state_copy #@UnresolvedImport
from westpa.kinetics import find_macrostate_transitions

# From w_stateprobs
from westpa.binning import accumulate_state_populations_from_labeled

# This block is responsible for submitting a set of calculations to be bootstrapped over for a particular type of calculation.
# A property which wishes to be calculated should adhere to this format.

def _rate_eval_block(iblock, start, stop, nstates, data_input, name, mcbs_alpha, mcbs_nsets, mcbs_acalpha, do_correl, **extra):
    # Our rate estimator is a little more complex, so we've defined a custom evaluation block for it,
    # instead of just using the block evalutors that we've imported.
    results = []
    for istate in xrange(nstates):
        for jstate in xrange(nstates):
            if istate == jstate: continue
            kwargs = { 'istate' : istate, 'jstate': jstate }
            # Why are we sending in the total population dataset, instead of a sliced one?
            # It's a requirement of our estimator; we need to pull from any given i to j state in order to properly normalize
            # and avoid i to j rate constants which are affected by a third state k.
            # That is, we need the populations for both i and j, and it's easier to just send in the entire dataset.
            dataset = {'dataset': data_input['dataset'][:, istate, jstate], 'pops': data_input['pops'] }
            ci_res = mcbs_ci_correl_rw(dataset,estimator=sequence_macro_flux_to_rate_bs,
                                    alpha=mcbs_alpha,n_sets=mcbs_nsets,autocorrel_alpha=mcbs_acalpha,
                                    subsample=numpy.mean, pre_calculated=data_input['rates'][:,istate,jstate][numpy.isfinite(data_input['rates'][:,istate,jstate])], do_correl=do_correl, **kwargs)
            results.append((name, iblock, istate, jstate, (start,stop) + ci_res))

    return results


# The old w_kinetics
class DKinetics(WESTKinAvg,WESTSubcommand):
    subcommand='kinetics'
    default_kinetics_file = 'direct.h5'
    default_output_file = 'direct.h5'
    help_text = 'calculate state-to-state kinetics by tracing trajectories'
    description = '''\
Calculate state-to-state rates and transition event durations by tracing
trajectories.

A bin assignment file (usually "assign.h5") including trajectory labeling
is required (see "w_assign --help" for information on generating this file).

The output generated by this program is used as input for the ``w_kinavg``
tool, which converts the flux data in the output file into average rates
with confidence intervals. See ``w_kinavg trace --help`` for more 
information.

-----------------------------------------------------------------------------
Output format
-----------------------------------------------------------------------------

The output file (-o/--output, by default "kintrace.h5") contains the
following datasets:

  ``/conditional_fluxes`` [iteration][state][state]
    *(Floating-point)* Macrostate-to-macrostate fluxes. These are **not**
    normalized by the population of the initial macrostate.

  ``/conditional_arrivals`` [iteration][stateA][stateB]
    *(Integer)* Number of trajectories arriving at state *stateB* in a given
    iteration, given that they departed from *stateA*.
    
  ``/total_fluxes`` [iteration][state]
    *(Floating-point)* Total flux into a given macrostate.
    
  ``/arrivals`` [iteration][state]
    *(Integer)* Number of trajectories arriving at a given state in a given
    iteration, regardless of where they originated.

  ``/duration_count`` [iteration]
    *(Integer)* The number of event durations recorded in each iteration.
    
  ``/durations`` [iteration][event duration]
    *(Structured -- see below)*  Event durations for transition events ending
    during a given iteration. These are stored as follows:
      
      istate
        *(Integer)* Initial state of transition event.
      fstate
        *(Integer)* Final state of transition event.
      duration
        *(Floating-point)* Duration of transition, in units of tau.
      weight
        *(Floating-point)* Weight of trajectory at end of transition, **not**
        normalized by initial state population.

Because state-to-state fluxes stored in this file are not normalized by
initial macrostate population, they cannot be used as rates without further
processing. The ``w_kinavg`` command is used to perform this normalization
while taking statistical fluctuation and correlation into account. See 
``w_kinavg trace --help`` for more information.  Target fluxes (total flux
into a given state) require no such normalization.

-----------------------------------------------------------------------------
Command-line options
-----------------------------------------------------------------------------
'''
    
    def __init__(self, parent):
        super(DKinetics,self).__init__(parent)

    def open_files(self):
        #self.output_file = h5io.WESTPAH5File(self.output_filename, 'w', creating_program=True)
        self.output_file = h5io.WESTPAH5File(self.output_filename, 'a', creating_program=True)
        h5io.stamp_creator_data(self.output_file)
        self.assignments_file = h5io.WESTPAH5File(self.assignments_filename, 'r')#, driver='core', backing_store=False)
        if not self.iter_range.check_data_iter_range_least(self.assignments_file):
            raise ValueError('assignments data do not span the requested iterations')

    def w_kinetics(self):
        pi = self.progress.indicator
        pi.new_operation('Initializing')
        with pi:
            self.data_reader.open('r')
            self.open_files()
            nstates = self.assignments_file.attrs['nstates']
            start_iter, stop_iter = self.iter_range.iter_start, self.iter_range.iter_stop # h5io.get_iter_range(self.assignments_file)
            iter_count = stop_iter - start_iter
            durations_ds = self.output_file.replace_dataset('durations', 
                                                           shape=(iter_count,0), maxshape=(iter_count,None),
                                                           dtype=ed_list_dtype,
                                                           chunks=(1,15360) if self.do_compression else None,
                                                           shuffle=self.do_compression,
                                                           compression=9 if self.do_compression else None)
            durations_count_ds = self.output_file.replace_dataset('duration_count',
                                                                 shape=(iter_count,), dtype=numpy.int_, shuffle=True,compression=9)
            cond_fluxes_ds = self.output_file.replace_dataset('conditional_fluxes',
                                                              shape=(iter_count,nstates,nstates), dtype=weight_dtype,
                                                              chunks=(h5io.calc_chunksize((iter_count,nstates,nstates),weight_dtype)
                                                                      if self.do_compression else None),
                                                              shuffle=self.do_compression,
                                                              compression=9 if self.do_compression else None)
            total_fluxes_ds = self.output_file.replace_dataset('total_fluxes',
                                                              shape=(iter_count,nstates), dtype=weight_dtype,
                                                              chunks=(h5io.calc_chunksize((iter_count,nstates),weight_dtype)
                                                                      if self.do_compression else None),
                                                              shuffle=self.do_compression,
                                                              compression=9 if self.do_compression else None)

            cond_arrival_counts_ds = self.output_file.replace_dataset('conditional_arrivals',
                                                                     shape=(iter_count,nstates,nstates), dtype=numpy.uint,
                                                                     chunks=(h5io.calc_chunksize((iter_count,nstates,nstates),
                                                                                                 numpy.uint)
                                                                             if self.do_compression else None),
                                                              shuffle=self.do_compression,
                                                              compression=9 if self.do_compression else None) 
            arrival_counts_ds = self.output_file.replace_dataset('arrivals',
                                                                shape=(iter_count,nstates), dtype=numpy.uint,
                                                                chunks=(h5io.calc_chunksize((iter_count,nstates),
                                                                                            numpy.uint)
                                                                        if self.do_compression else None),
                                                                shuffle=self.do_compression,
                                                                compression=9 if self.do_compression else None)

            # copy state labels for convenience
            self.output_file.replace_dataset('state_labels', data=self.assignments_file['state_labels'][...])

            # Put nice labels on things
            for ds in (self.output_file, durations_count_ds, cond_fluxes_ds, total_fluxes_ds):
                h5io.stamp_iter_range(ds, start_iter, stop_iter)

            # Calculate instantaneous rate matrices and trace trajectories
            last_state = None
            pi.new_operation('Tracing trajectories', iter_count)
            for iiter, n_iter in enumerate(xrange(start_iter, stop_iter)):
                # Get data from the main HDF5 file
                iter_group = self.data_reader.get_iter_group(n_iter)
                seg_index = iter_group['seg_index']
                nsegs, npts = iter_group['pcoord'].shape[0:2] 
                weights = seg_index['weight']
                #parent_ids = seg_index['parent_id']
                parent_ids = self.data_reader.parent_id_dsspec.get_iter_data(n_iter)
                
                # Get bin and traj. ensemble assignments from the previously-generated assignments file
                assignment_iiter = h5io.get_iteration_entry(self.assignments_file, n_iter)
                bin_assignments = numpy.require(self.assignments_file['assignments'][assignment_iiter + numpy.s_[:nsegs,:npts]],
                                                dtype=index_dtype)
                label_assignments = numpy.require(self.assignments_file['trajlabels'][assignment_iiter + numpy.s_[:nsegs,:npts]],
                                                  dtype=index_dtype)
                state_assignments = numpy.require(self.assignments_file['statelabels'][assignment_iiter + numpy.s_[:nsegs,:npts]],
                                                  dtype=index_dtype)
                
                # Prepare to run analysis
                cond_fluxes = numpy.zeros((nstates,nstates), weight_dtype)
                total_fluxes = numpy.zeros((nstates,), weight_dtype)
                cond_counts = numpy.zeros((nstates,nstates), numpy.uint)
                total_counts = numpy.zeros((nstates,), numpy.uint)
                durations = []
    
                # Estimate macrostate fluxes and calculate event durations using trajectory tracing
                # state is opaque to the find_macrostate_transitions function            
                state = _fast_transition_state_copy(iiter, nstates, parent_ids, last_state)
                find_macrostate_transitions(nstates, weights, label_assignments, state_assignments, 1.0/(npts-1), state,
                                            cond_fluxes, cond_counts, total_fluxes, total_counts, durations)
                last_state = state
                
                # Store trace-based kinetics data
                cond_fluxes_ds[iiter] = cond_fluxes
                total_fluxes_ds[iiter] = total_fluxes
                arrival_counts_ds[iiter] = total_counts
                cond_arrival_counts_ds[iiter] = cond_counts
                
                durations_count_ds[iiter] = len(durations)
                if len(durations) > 0:
                    durations_ds.resize((iter_count, max(len(durations), durations_ds.shape[1])))
                    durations_ds[iiter,:len(durations)] = durations
                        
                # Do a little manual clean-up to prevent memory explosion
                del iter_group, weights, parent_ids, bin_assignments, label_assignments, state, cond_fluxes, total_fluxes
                pi.progress += 1
    def go(self):
        self.w_kinetics()

# The old w_kinavg
class DKinAvg(AverageCommands):
    subcommand = 'kinavg'
    help_text = 'averages and CIs for path-tracing kinetics analysis'
    default_kinetics_file = 'direct.h5'
    description = '''\
Calculate average rates and associated errors from weighted ensemble data. Bin
assignments (usually "assignments.h5") and kinetics data (usually
"kintrace.h5" or "kinmat.h5") data files must have been previously generated
(see "w_assign --help" and "w_kinetics --help" for information on generating
these files).

-----------------------------------------------------------------------------
Output format
-----------------------------------------------------------------------------

The output file (-o/--output, usually "kinavg.h5") contains the following
dataset:

  /avg_rates [state,state]
    (Structured -- see below) State-to-state rates based on entire window of
    iterations selected.

For trace mode, the following additional datasets are generated:

  /avg_total_fluxes [state]
    (Structured -- see below) Total fluxes into each state based on entire
    window of iterations selected.
    
  /avg_conditional_fluxes [state,state]
    (Structured -- see below) State-to-state fluxes based on entire window of
    iterations selected.

If --evolution-mode is specified, then the following additional dataset is
available:

  /rate_evolution [window][state][state]
    (Structured -- see below). State-to-state rates based on windows of
    iterations of varying width.  If --evolution-mode=cumulative, then
    these windows all begin at the iteration specified with
    --start-iter and grow in length by --step-iter for each successive 
    element. If --evolution-mode=blocked, then these windows are all of
    width --step-iter (excluding the last, which may be shorter), the first
    of which begins at iteration --start-iter.
    
If --evolution-mode is specified in trace mode, the following additional
datasets are available:

  /target_flux_evolution [window,state]
    (Structured -- see below). Total flux into a given macro state based on
    windows of iterations of varying width, as in /rate_evolution.
    
  /conditional_flux_evolution [window,state,state]
    (Structured -- see below). State-to-state fluxes based on windows of
    varying width, as in /rate_evolution.
    
The structure of these datasets is as follows:

  iter_start
    (Integer) Iteration at which the averaging window begins (inclusive).
    
  iter_stop
    (Integer) Iteration at which the averaging window ends (exclusive).
    
  expected
    (Floating-point) Expected (mean) value of the rate as evaluated within
    this window, in units of inverse tau.
    
  ci_lbound
    (Floating-point) Lower bound of the confidence interval on the rate
    within this window, in units of inverse tau.
    
  ci_ubound
    (Floating-point) Upper bound of the confidence interval on the rate 
    within this window, in units of inverse tau.
    
  corr_len
    (Integer) Correlation length of the rate within this window, in units
    of tau.

Each of these datasets is also stamped with a number of attributes:

  mcbs_alpha
    (Floating-point) Alpha value of confidence intervals. (For example, 
    *alpha=0.05* corresponds to a 95% confidence interval.)

  mcbs_nsets
    (Integer) Number of bootstrap data sets used in generating confidence
    intervals.
    
  mcbs_acalpha
    (Floating-point) Alpha value for determining correlation lengths.
   

-----------------------------------------------------------------------------
Command-line options
-----------------------------------------------------------------------------
'''

    def w_kinavg(self):
        pi = self.progress.indicator

        # We're initializing the various datasets...
        if True:
            self.open_files()
            self.open_assignments()
            # Obviously, this is for the conditional and total fluxes.  This is really all we need to sort for this.
            cond_fluxes = h5io.IterBlockedDataset(self.kinetics_file['conditional_fluxes'])
            cond_fluxes.cache_data()
            total_fluxes = h5io.IterBlockedDataset(self.kinetics_file['total_fluxes'])
            

            # This is necessary for both color and state populations...
            # ... but we also need this for the kinetics calculations.
            pops = h5io.IterBlockedDataset(self.assignments_file['labeled_populations'])
            pops.cache_data()
            pops.data = pops.data.sum(axis=2)

            # Here, we're pre-generating the information needed...
            rates = h5io.IterBlockedDataset.empty_like(cond_fluxes)
            rates.data = sequence_macro_flux_to_rate(cond_fluxes.data, pops.data[:self.nstates,:self.nbins])

            # As the dataset, just send in the rates and such for now.

        submit_kwargs = dict(pi=pi, nstates=self.nstates, start_iter=self.start_iter, stop_iter=self.stop_iter, 
                             step_iter=self.step_iter)

        # Calculate averages for the simulation, then report, if necessary.
        with pi:
            submit_kwargs['dataset'] = {'dataset': cond_fluxes, 'pops': pops, 'rates': rates}
            avg_rates = self.run_calculation(eval_block=_rate_eval_block, name='Rate Evolution', dim=2, do_averages=True, **submit_kwargs)
            self.output_file.replace_dataset('avg_rates', data=avg_rates[1])

            submit_kwargs['dataset'] = {'dataset': cond_fluxes }
            avg_conditional_fluxes = self.run_calculation(eval_block=_2D_simple_eval_block, name='Conditional Flux Evolution', dim=2, do_averages=True, **submit_kwargs)
            self.output_file.replace_dataset('avg_conditional_fluxes', data=avg_conditional_fluxes[1])

            submit_kwargs['dataset'] = {'dataset': total_fluxes }
            avg_total_fluxes = self.run_calculation(eval_block=_1D_simple_eval_block, name='Target Flux Evolution', dim=1, do_averages=True, **submit_kwargs)
            self.output_file.replace_dataset('avg_total_fluxes', data=avg_total_fluxes[1])

        # Now, print them!
        pi.clear()

        # We've returned an average, but it still exists in a timeslice format.  So we need to return the 'last' value.
        self.print_averages(avg_total_fluxes[1], '\nfluxes into macrostates:', dim=1)
        self.print_averages(avg_conditional_fluxes[1], '\nfluxes from state to state:', dim=2)
        self.print_averages(avg_rates[1], '\nrates from state to state:', dim=2)

        # Do a bootstrap evolution.
        pi.clear()
        with pi:
            submit_kwargs['dataset'] = {'dataset': cond_fluxes, 'pops': pops, 'rates': rates}
            rate_evol = self.run_calculation(eval_block=_rate_eval_block, name='Rate Evolution', dim=2, **submit_kwargs)
            self.output_file.replace_dataset('rate_evolution', data=rate_evol, shuffle=True, compression=9)
            pi.clear()

            submit_kwargs['dataset'] = {'dataset': cond_fluxes }
            rate_evol = self.run_calculation(eval_block=_2D_simple_eval_block, name='Conditional Flux Evolution', dim=2, **submit_kwargs)
            self.output_file.replace_dataset('conditional_flux_evolution', data=rate_evol, shuffle=True, compression=9)
            pi.clear()

            submit_kwargs['dataset'] = {'dataset': total_fluxes }
            rate_evol = self.run_calculation(eval_block=_1D_simple_eval_block, name='Target Flux Evolution', dim=1, **submit_kwargs)
            self.output_file.replace_dataset('target_flux_evolution', data=rate_evol, shuffle=True, compression=9)
            pi.clear()

    def go(self):
        self.w_kinavg()


# The old w_stateprobs
class DStateProbs(AverageCommands):
    subcommand = 'stateprobs'
    help_text = 'averages and CIs for path-tracing kinetics analysis'
    default_kinetics_file = 'direct.h5'
    description = '''\
Calculate average populations and associated errors in state populations from
weighted ensemble data. Bin assignments, including macrostate definitions,
are required. (See "w_assign --help" for more information).

-----------------------------------------------------------------------------
Output format
-----------------------------------------------------------------------------

The output file (-o/--output, usually "stateprobs.h5") contains the following
dataset:

  /avg_state_pops [state]
    (Structured -- see below) Population of each state across entire
    range specified.

If --evolution-mode is specified, then the following additional dataset is
available:

  /state_pop_evolution [window][state]
    (Structured -- see below). State populations based on windows of
    iterations of varying width.  If --evolution-mode=cumulative, then
    these windows all begin at the iteration specified with
    --start-iter and grow in length by --step-iter for each successive 
    element. If --evolution-mode=blocked, then these windows are all of
    width --step-iter (excluding the last, which may be shorter), the first
    of which begins at iteration --start-iter.
    
The structure of these datasets is as follows:

  iter_start
    (Integer) Iteration at which the averaging window begins (inclusive).
    
  iter_stop
    (Integer) Iteration at which the averaging window ends (exclusive).
    
  expected
    (Floating-point) Expected (mean) value of the rate as evaluated within
    this window, in units of inverse tau.
    
  ci_lbound
    (Floating-point) Lower bound of the confidence interval on the rate
    within this window, in units of inverse tau.
    
  ci_ubound
    (Floating-point) Upper bound of the confidence interval on the rate 
    within this window, in units of inverse tau.
    
  corr_len
    (Integer) Correlation length of the rate within this window, in units
    of tau.

Each of these datasets is also stamped with a number of attributes:

  mcbs_alpha
    (Floating-point) Alpha value of confidence intervals. (For example, 
    *alpha=0.05* corresponds to a 95% confidence interval.)

  mcbs_nsets
    (Integer) Number of bootstrap data sets used in generating confidence
    intervals.
    
  mcbs_acalpha
    (Floating-point) Alpha value for determining correlation lengths.
   

-----------------------------------------------------------------------------
Command-line options
-----------------------------------------------------------------------------
'''    

    def calculate_state_populations(self, pops):
            # ... but then this is how the state populations are done.
            # This was taken, more or less, from the old w_stateprobs
            iter_count = self.stop_iter-self.start_iter
            all_state_pops = numpy.empty((iter_count,self.nstates+1), weight_dtype)
            iter_state_pops = numpy.empty((self.nstates+1,), weight_dtype)
            avg_state_pops = numpy.zeros((self.nstates+1,), weight_dtype)
            pops.cache_data(max_size='available')
            state_map = self.assignments_file['state_map'][...]
            try:
                for iiter,n_iter in enumerate(xrange(self.start_iter,self.stop_iter)):
                    iter_state_pops.fill(0)
                    labeled_pops = pops.iter_entry(n_iter)
                    accumulate_state_populations_from_labeled(labeled_pops, state_map, iter_state_pops, check_state_map=False)
                    all_state_pops[iiter] = iter_state_pops
                    avg_state_pops += iter_state_pops
                    del labeled_pops
            finally:
                pops.drop_cache()

            state_pops = h5io.IterBlockedDataset.empty_like(pops)
            state_pops.data = all_state_pops
            return state_pops

    def w_stateprobs(self):
        pi = self.progress.indicator
        if True:
            self.open_files()
            self.open_assignments()
            # So far, we definitely need this boilerplate...
            #pi.new_operation('Reading data')

            # This is necessary for both color and state populations...
            pops = h5io.IterBlockedDataset(self.assignments_file['labeled_populations'])

            state_pops = self.calculate_state_populations(pops)

            # This now sorts it for the color populations
            pops.cache_data()
            pops.data = pops.data.sum(axis=2)

        submit_kwargs = dict(pi=pi,nstates=self.nstates, start_iter=self.start_iter, stop_iter=self.stop_iter, 
                             step_iter=self.step_iter, eval_block=_1D_simple_eval_block)

        # Calculate and print averages
        with pi:
            submit_kwargs['dataset'] = {'dataset': pops}
            color_evol_avg = self.run_calculation(name='Color Probability Evolution', dim=1, do_averages=True, **submit_kwargs)
            self.output_file.replace_dataset('avg_color_probs', data=color_evol_avg[1], shuffle=True, compression=9)

            submit_kwargs['dataset'] = {'dataset': state_pops}
            state_evol_avg = self.run_calculation(name='State Probability Evolution', dim=1, do_averages=True, **submit_kwargs)
            self.output_file.replace_dataset(name='avg_state_probs', data=state_evol_avg[1], shuffle=True, compression=9)

        # Print!
        self.print_averages(color_evol_avg[1], '\naverage color probabilities:', dim=1)
        self.print_averages(state_evol_avg[1], '\naverage state probabilities:', dim=1)

        # Now, do a bootstrap evolution
        with pi:
            submit_kwargs['dataset'] = {'dataset': pops}
            pop_evol = self.run_calculation(name='Color Probability Evolution', dim=1, **submit_kwargs)
            self.output_file.replace_dataset('color_prob_evolution', data=pop_evol, shuffle=True, compression=9)

            submit_kwargs['dataset'] = {'dataset': state_pops}
            pop_evol = self.run_calculation(name='State Probability Evolution', dim=1, **submit_kwargs)
            self.output_file.replace_dataset(name='state_pop_evolution', data=pop_evol, shuffle=True, compression=9)

    def go(self):
        self.w_stateprobs()

# Just a convenience class to run everything.
class DAll(DStateProbs, DKinAvg, DKinetics):
    subcommand = 'all'
    help_text = 'averages and CIs for path-tracing kinetics analysis'
    default_kinetics_file = 'direct.h5'

    def go(self):
        # One minor issue; as this stands now, since it's inheriting from all the other classes, it needs
        # a kinetics file to instantiate the other attributes.  We'll need to modify how the loading works, there.
        self.w_kinetics()
        self.w_kinavg()
        self.w_stateprobs()

# Just a convenience class to average the observables.
class DAverage(DStateProbs, DKinAvg):
    subcommand = 'average'
    help_text = 'averages and CIs for path-tracing kinetics analysis'
    default_kinetics_file = 'direct.h5'

    def go(self):
        self.w_kinavg()
        self.w_stateprobs()


class WDirect(WESTMasterCommand, WESTParallelTool):
    prog='w_direct'
    #subcommands = [AvgTraceSubcommand,AvgMatrixSubcommand]
    subcommands = [DKinetics, DKinAvg, DStateProbs, DAll, DAverage]
    subparsers_title = 'direct kinetics analysis schemes'

if __name__ == '__main__':
    WDirect().main()
