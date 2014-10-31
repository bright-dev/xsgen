from __future__ import print_function
import os
import subprocess

import numpy as np

from xsgen.statepoint import StatePoint

from pyne import rxname
from pyne import nucname
from pyne import origen22
from pyne.material import Material
from pyne.xs import data_source
from pyne.xs.cache import XSCache

from xsgen.utils import indir, NotSpecified

# templates are from openmc/examples/lattice/simple

SETTINGS_TEMPLATE = """<?xml version="1.0"?>
<settings>
  <cross_sections>{openmc_cross_sections}</cross_sections>
  <eigenvalue>
    <batches>{k_cycles}</batches>
    <inactive>{k_cycles_skip}</inactive>
    <particles>{k_particles}</particles>
  </eigenvalue>
  <source>
    <space type="box">
      <parameters>0 0 0 {unit_cell_height} {unit_cell_height} {unit_cell_height}</parameters>
    </space>
  </source>
</settings>
"""

MATERIALS_TEMPLATE = """<?xml version="1.0"?>
<materials>
  <default_xs>71c</default_xs>
  <material id="1">
    <density value="{fuel_density}" units="g/cc" />
    {_fuel_nucs}
  </material>
  <material id="2">
    <density value="{cool_density}" units="g/cc" />
    {_cool_nucs}
    <sab name="HH2O" xs="71t" />
  </material>
  <material id="3">
    <density value="{clad_density}" units="g/cc" />
    {_clad_nucs}
  </material>
</materials>
"""

GEOMETRY_TEMPLATE = """<?xml version="1.0"?>
<geometry>
  <cell id="1" fill="5" surfaces="1 -2 3 -4" />
  <cell id="101" universe="1" material="1" surfaces="-5" />
  <cell id="102" universe="1" material="void" surfaces="5 -6" />
  <cell id="103" universe="1" material="3" surfaces="6 -7" />
  <cell id="104" universe="1" material="2" surfaces="7" />
  <cell id="201" universe="2" material="2" surfaces="8" />
  <cell id="302" universe="3" material="3" surfaces="9" />
  <lattice id="5">
    <type>rectangular</type>
    <dimension>{_latt_shape0} {_latt_shape1}</dimension>
    <lower_left>-{_latt_x_half_pitch} -{_latt_y_half_pitch}</lower_left>
    <width>{unit_cell_pitch} {unit_cell_pitch}</width>
    <universes>
      {lattice}
    </universes>
  </lattice>
  <surface id="1" type="x-plane" coeffs="-{_latt_x_half_pitch}" boundary="reflective" />
  <surface id="2" type="x-plane" coeffs="{_latt_x_half_pitch}" boundary="reflective" />
  <surface id="3" type="y-plane" coeffs="-{_latt_y_half_pitch}" boundary="reflective" />
  <surface id="4" type="y-plane" coeffs="{_latt_y_half_pitch}" boundary="reflective" />
  <surface id="5" type="z-cylinder" coeffs="0.0 0.0 {fuel_cell_radius}" />
  <surface id="6" type="z-cylinder" coeffs="0.0 0.0 {void_cell_radius}" />
  <surface id="7" type="z-cylinder" coeffs="0.0 0.0 {clad_cell_radius}" />
  <surface id="8" type="z-cylinder" coeffs="0.0 0.0 0.0" />
  <surface id="9" type="z-cylinder" coeffs="0.0 0.0 0.0" />
</geometry>
"""

TALLIES_TEMPLATE = """<?xml version="1.0"?>
<tallies>
  <tally id="1">
    <label>flux</label>
    <filter type="energy" bins="{_egrid}" />
    <filter type="material" bins="1" />
    <scores>flux</scores>
    <nuclides>total</nuclides>
  </tally>
  <tally id="2">
    <label>cinderflux</label>
    <filter type="energy" bins="{_cds_egrid}" />
    <filter type="material" bins="1" />
    <scores>flux</scores>
    <nuclides>total</nuclides>
  </tally>
  <tally id="3">
    <label>eafflux</label>
    <filter type="energy" bins="{_eafds_egrid}" />
    <filter type="material" bins="1" />
    <scores>flux</scores>
    <nuclides>total</nuclides>
  </tally>
  <tally id="4">
    <label>omcflux</label>
    <filter type="energy" bins="{_omcds_egrid}" />
    <filter type="material" bins="1" />
    <scores>flux</scores>
    <nuclides>total</nuclides>
  </tally>
  <tally id="5">
    <label>s_gh</label>
    <filter type="energy" bins="{_egrid}" />
    <filter type="energyout" bins="{_egrid}" />
    <filter type="material" bins="1" />
    <scores>scatter</scores>
    <nuclides>total</nuclides>
  </tally>
</tallies>
"""

PLOTS_TEMPLATE = """<?xml version="1.0"?>
<plots>
  <plot id="1" color="mat">
    <origin>0. 0. 0.</origin>
    <width>{_latt_x_pitch} {_latt_y_pitch}</width>
    <pixels>600 600</pixels>
  </plot>
</plots>
"""

class OpenMCOrigen(object):
    """An that combines OpenMC for k-code calculations and ORIGEN for 
    transmutation.
    """

    reactions = {rxname.id(_) for _ in ('total', 'absorption', 'gamma', 'gamma_1', 
                 'z_2n', 'z_2n_1', 'z_3n', 'proton', 'alpha', 'fission')}

    def __init__(self, rc):
        self.rc = rc
        self.statelibs = {}
        self.builddir = 'build-' + rc.reactor
        if not os.path.isdir(self.builddir):
            os.makedirs(self.builddir)
        self.cinderds = data_source.CinderDataSource()
        self.eafds = data_source.EAFDataSource()
        self.omcds = data_source.OpenMCDataSource(
                        cross_sections=rc.openmc_cross_sections,
                        #src_group_struct=self.eafds.src_group_struct)
                        src_group_struct=np.logspace(1, -9, 1001))
        data_sources = [self.omcds]
        if not rc.is_thermal:
            data_sources.append(self.eafds)
        data_sources += [self.cinderds, data_source.SimpleDataSource(),
                         data_source.NullDataSource()]
        for ds in data_sources[1:]:
            ds.load()
        self.xscache = XSCache(data_sources=data_sources)

    def pwd(self, state, directory):
        return os.path.join(self.builddir, str(hash(state)), directory)

    def context(self, state):
        rc = self.rc
        ctx = dict(rc._dict)
        ctx.update(zip(rc.perturbation_params, state))
        return ctx

    def generate_run(self, run):
        mat_hist = []
        run_lib = {"TIME": [], "NEUT_PROD": [], "NEUT_DEST": [], "BUd":  []}
        for i, state in enumerate(run):
            if i < len(run) - 1 :
                transmute_time = run[i+1].burn_times - state.burn_times
            else:
                transmute_time = 0
            k, phi_g, xs, mat = self.generate(state, transmute_time)
            run_lib["TIME"].append(state.burn_times)
            run_lib["NEUT_PROD"].append(0)
            run_lib["NEUT_DEST"].append(0)
            run_lib["BUd"].append(0)
            mat_hist.append(mat)
        nucs = []
        for mat in mat_hist:
            nucs.extend(mat.comp.keys())
        nucs = set(nucs)
        nucs = [(nucname.name(nuc), [mat.comp[nuc] for mat in mat_hist]) for nuc in nucs]
        nucs = dict(nucs)

        run_lib.update(nucs)

        return run_lib
        # return mat_hist


    def generate(self, state, transmute_time):
        """Generates a library for a given state."""

        if state in self.statelibs:
            return self.statelibs[state]
        k, phi_g, xs = self.openmc(state)
        self.statelibs[state] = (k, phi_g, xs)
        self.rc.fuel_material = self.origen(state, xs, transmute_time, phi_g)
        # store what has become of each tracked nuclide
        # run origen on 1 kg of each nuclide, write out
        return (k, phi_g, xs, self.rc.fuel_material)

    def openmc(self, state):
        """Runs OpenMC for a given state."""
        # make inputs
        pwd = self.pwd(state, "omc")
        if not os.path.isdir(pwd):
            os.makedirs(pwd)
        self._make_omc_input(state)
        # run openmc
        statepoint = _find_statepoint(pwd)
        if statepoint is None:
            with indir(pwd):
                subprocess.check_call(['openmc'])
            statepoint = _find_statepoint(pwd)
        # parse & prepare results
        k, phi_g = self._parse_statepoint(statepoint)
        xstab = self._generate_xs(phi_g)
        return k, phi_g, xstab

    def _make_omc_input(self, state):
        pwd = self.pwd(state, "omc")
        ctx = self.context(state)
        rc = self.rc
        # settings
        settings = SETTINGS_TEMPLATE.format(**ctx)
        with open(os.path.join(pwd, 'settings.xml'), 'w') as f:
            f.write(settings)
        # materials
        valid_nucs = self.nucs_in_cross_sections()
        # core_nucs = set(ctx['core_transmute'])
        ctx['_fuel_nucs'] = _mat_to_nucs(rc.fuel_material[valid_nucs])
        ctx['_clad_nucs'] = _mat_to_nucs(rc.clad_material[valid_nucs])
        ctx['_cool_nucs'] = _mat_to_nucs(rc.cool_material[valid_nucs])
        materials = MATERIALS_TEMPLATE.format(**ctx)
        with open(os.path.join(pwd, 'materials.xml'), 'w') as f:
            f.write(materials)
        # geometry
        ctx['lattice'] = ctx['lattice'].strip().replace('\n', '\n      ')
        ctx['_latt_shape0'] = ctx['lattice_shape'][0]
        ctx['_latt_shape1'] = ctx['lattice_shape'][1]
        ctx['_latt_x_pitch'] = ctx['unit_cell_pitch'] * ctx['lattice_shape'][0]
        ctx['_latt_y_pitch'] = ctx['unit_cell_pitch'] * ctx['lattice_shape'][1]
        ctx['_latt_x_half_pitch'] = ctx['_latt_x_pitch'] / 2.0
        ctx['_latt_y_half_pitch'] = ctx['_latt_y_pitch'] / 2.0
        geometry = GEOMETRY_TEMPLATE.format(**ctx)
        with open(os.path.join(pwd, 'geometry.xml'), 'w') as f:
            f.write(geometry)
        # tallies
        ctx['_egrid'] = " ".join(map(str, sorted(ctx['group_structure'])))
        # ctx['_cds_egrid'] = " ".join(map(str, sorted(self.cinderds.src_group_struct)))
        ctx['_cds_egrid'] = " 1 10 100 1000" # I don't have cinder
        ctx['_eafds_egrid'] = " ".join(map(str, sorted(self.eafds.src_group_struct)))
        ctx['_omcds_egrid'] = " ".join(map(str, sorted(self.omcds.src_group_struct)))
        # nucs = core_nucs & valid_nucs
        # ctx['_nucs'] = " ".join([nucname.serpent(nuc) for nuc in sorted(nucs)])
        tallies = TALLIES_TEMPLATE.format(**ctx)
        with open(os.path.join(pwd, 'tallies.xml'), 'w') as f:
            f.write(tallies)
        # plots
        plots = PLOTS_TEMPLATE.format(**ctx)
        with open(os.path.join(pwd, 'plots.xml'), 'w') as f:
            f.write(plots)

    def nucs_in_cross_sections(self):
        """Returns the set of nuclides present in the cross_sections.xml file.
        """
        return {n.nucid for n in self.omcds.cross_sections.ace_tables \
                if n.nucid is not None}

    def _parse_statepoint(self, statepoint):
        """Parses a statepoint file and reads in the relevant fluxes, assigns them 
        to the DataSources or the XSCache, and returns k and phi_g.
        """
        sp = StatePoint(statepoint)
        sp.read_results()
        # compute group fluxes for data sources
        for tally, ds in zip(sp.tallies[1:4], (self.cinderds, self.eafds, self.omcds)):
            ds.src_phi_g = tally.results[::-1, :, 0].flatten()
            ds.src_phi_g /= ds.src_phi_g.sum()
        # compute return values
        k, kerr = sp.k_combined
        t_flux = sp.tallies[0]
        phi_g = t_flux.results[::-1, :, 0].flatten()
        phi_g /= phi_g.sum()
        return k, phi_g

    def _generate_xs(self, phi_g):
        rc = self.rc
        verbose = rc.verbose
        xscache = self.xscache
        xscache['E_g'] = rc.group_structure
        xscache['phi_g'] = phi_g
        G = len(phi_g)
        temp = rc.temperature
        rxs = self.reactions
        nucs = rc.track_nucs
        dt = np.dtype([('nuc', 'i4'), ('rx', np.uint32), ('xs', 'f8', G)])
        data = np.empty(len(nucs)*len(rxs), dtype=dt)
        i = 0
        for nuc in nucs:
            for rx in rxs:
                xs = xscache[nuc, rx, temp]
                if verbose:
                    print("OpenMC XS:", nucname.name(nuc), rxname.name(rx), xs)
                data[i] = nuc, rx, xs
                i += 1
        return data

    def origen(self, state, xs, transmute_time, phi_g):
        # """Runs ORIGEN calulations to obtain transmutation matrix."""
        """Uses arithmetic to obtain transmutation matrix"""
        pwd = self.pwd(state, "origen")
        if not os.path.isdir(pwd):
            os.makedirs(pwd)
        self._make_origen_input(state, transmute_time, phi_g)

        if self.rc.origen_call is NotSpecified:
            if self.rc.is_thermal:
                origen_call = "o2_therm_linux.exe"
            else:
                origen_call = "o2_fast_linux.exe"
        else:
            origen_call = self.rc.origen_call

        with indir(pwd):
            subprocess.check_call(origen_call)
            tape6 = origen22.parse_tape6("TAPE6.OUT")
            return tape6.materials[-1]

    def _make_origen_input(self, state, transmute_time, phi_g):
        pwd = self.pwd(state, "origen")
        ctx = self.context(state)
        with indir(pwd):
            # may need to filter tape4 for Bad Nuclides
            origen22.write_tape4(self.rc.fuel_material)
            total_flux = phi_g.sum()
            origen22.write_tape5_irradiation("IRF", transmute_time, total_flux)
            tape9 = origen22.make_tape9(self.rc.track_nucs)
            import pdb; pdb.set_trace()
            origen22.write_tape9(tape9)


def _mat_to_nucs(mat):
    nucs = []
    template = '<nuclide name="{nuc}" wo="{mass}" />'
    for nuc, mass in mat.comp.items():
        nucs.append(template.format(nuc=nucname.serpent(nuc), mass=mass*100))
    nucs = "\n    ".join(nucs)
    return nucs

def _find_statepoint(pwd):
    for f in os.listdir(pwd):
        if f.startswith('statepoint'):
            return os.path.join(pwd, f)
    return 
