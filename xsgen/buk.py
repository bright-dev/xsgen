"""Plugin that runs burnup-criticality calculation.

Provides the following command-line arguments:
  - ``--openmc-cross-sections``: Path to the cross_sections.xml file for OpenMC
  - ``--origen``: ORIGEN 2.2 command
  - ``--solver``: The physics codes that are used to solve the burnup-criticality problem and compute cross sections and transmutation matrices.

Burnup-criticality plugin API
=============================
"""
from __future__ import print_function
import os
import shutil

import numpy as np

from xsgen.plugins import Plugin
from xsgen.utils import RunControl, NotSpecified
from xsgen.openmc_origen import OpenMCOrigen

SOLVER_ENGINES = {'openmc+origen': OpenMCOrigen}


class XSGenPlugin(Plugin):
    """The plugin class, inheriting from xsgen.plugins.Plugin."""

    requires = ('xsgen.pre',)
    """The burnup-criticality plugin requires :mod:`xsgen.pre`."""

    defaultrc = RunControl(
        solver=NotSpecified,
        openmc_cross_sections=NotSpecified,
        openmc_group_struct=np.logspace(1, -9, 1001),
        )

    rcdocs = {
        'openmc_cross_sections': ('Path to the cross_sections.xml file '
                                  'for OpenMC'),
        'openmc_group_struct': 'Group structure for OpenMC data source.',
        'origen': 'ORIGEN 2.2 command',
        'threads': 'Number of threads to use',
        'solver': ('The physics codes that are used to solve the '
                   'burnup-criticality problem and compute cross sections and '
                   'transmutation matrices.'),
        'plot_group_flux': 'Output plots of group flux for each OpenMC run.',
        }

    def update_argparser(self, parser):
        """Adds plugin-specific command-line arguments.

        Parameters
        ----------
        parser : argparse.ArgumentParser
            The parser that belongs to xsgen. We update this.

        Returns
        -------
        None
        """
        parser.add_argument('--threads', '-j', dest='threads', help=self.rcdocs['threads'],
                            type=int)
        parser.add_argument('--solver', dest='solver', help=self.rcdocs['solver'])
        parser.add_argument('--origen', dest='origen_call', help=self.rcdocs['origen'])
        parser.add_argument("--openmc-cross-sections", dest="openmc_cross_sections",
                            help=self.rcdocs['openmc_cross_sections'])
        parser.add_argument("--plot-group-flux", dest="plot_group_flux", action="store_true",
                            help=self.rcdocs["plot_group_flux"])

    def setup(self, rc):
        """Check if we have OpenMC cross-section data in the RC and set the appropriate
        physics code in rc.engine.

        Parameters
        ----------
        rc : xsgen.utils.RunControl
            The RunControl that controls this instance of xsgen.

        Returns
        -------
        None
        """
        self._ensure_omcxs(rc)

        # do after all other values have been setup
        if rc.solver is NotSpecified:
            raise ValueError('a solver type must be specified')
        rc.engine = SOLVER_ENGINES[rc.solver](rc)
        if rc.clean and os.path.isdir(rc.engine.builddir):
            shutil.rmtree(rc.engine.builddir, ignore_errors=True)
            print("removing builddir")

    def same_except_burnup_time(self, state1, state2):
        """Check if two different states are equivalent except for their burnup time.

        Parameters
        ----------
        state1, state2 : namedtuple (State)
            The states to compare.

        Returns
        -------
        bool
            True if the two state are the same except burnup time, else False.
        """
        if len(state1) != len(state2):
            raise ValueError("States have unequal number of perturbation paramaters.")
        for index in range(len(state1)):
            if state1._fields[index] == 'burn_times':
                continue
            if state1[index] != state2[index]:
                return False
        return True

    def execute(self, rc):
        """Sort states into runs by initial parameters, then generate libraries
        for each run and write them to an output file.

        Parameters
        ----------
        rc : xsgen.utils.RunControl
            The RunControl controlling this instance of xsgen.

        Returns
        -------
        None
        """
        runs = []
        for state in rc.states:
            already_existed = False
            for run in runs:
                if self.same_except_burnup_time(run[0], state):
                    run.append(state)
                    already_existed = True
            if not already_existed:
                runs.append([state])
        rc.runs = runs

        for run_num, run in enumerate(rc.runs):
            basepath = os.path.join(rc.engine.builddir, rc.outdirs[0])
            fname = basepath + str(run_num)
            libs = rc.engine.generate_run(run, fname)
            for i, writer in enumerate(rc.writers):
                basepath = os.path.join(rc.engine.builddir, rc.outdirs[0])
                fname = basepath + str(run_num)
                writer.write(libs, fname)

    #
    # ensure functions
    #

    def _ensure_omcxs(self, rc):
        """Ensure that rc.openmc_cross_sections is defined and valid.

        Parameters
        ----------
        rc : xsgen.utils.RunControl
            A RunControl instance holding the run control parameters of this
            instance of xsgen.

        Returns
        -------
        None
        """
        if rc.openmc_cross_sections is not NotSpecified: # which means Specified
            rc.openmc_cross_sections = os.path.abspath(rc.openmc_cross_sections)
        elif 'CROSS_SECTIONS' in os.environ:
            rc.openmc_cross_sections = os.path.abspath(os.environ['CROSS_SECTIONS'])
        else:
            rc.openmc_cross_sections = None
