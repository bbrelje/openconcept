from __future__ import division
from openmdao.api import Problem, Group, IndepVarComp, NewtonSolver, DirectSolver, BoundsEnforceLS
from openmdao.api import ScipyOptimizeDriver, ExplicitComponent, ImplicitComponent

import numpy as np
import scipy.sparse as sp
import sys, os
sys.path.insert(0,os.getcwd())
from openconcept.components.ducts import ImplicitCompressibleDuct
from openconcept.utilities.math.integrals import Integrator
from openconcept.utilities.math.derivatives import FirstDerivative
from openconcept.utilities.math import AddSubtractComp, ElementMultiplyDivideComp, VectorConcatenateComp, VectorSplitComp
from openconcept.analysis.atmospherics.compute_atmos_props import ComputeAtmosphericProperties


"""Analysis routines for simulating thermal management of aircraft components"""


class ThermalComponentWithMass(ExplicitComponent):
    """
    Computes thermal residual of a component with heating, cooling, and thermal mass

    Inputs
    ------
    q_in : float
        Heat generated by the component (vector, W)
    q_out : float
        Heat to waste stream (vector, W)
    mass : float
        Thermal mass (scalar, kg)

    Outputs
    -------
    dTdt : float
        First derivative of temperature (vector, K/s)

    Options
    -------
    specific_heat : float
        Specific heat capacity of the object in J / kg / K (default 921 = aluminum)
    num_nodes : float
        The number of analysis points to run
    """
    def initialize(self):
        self.options.declare('num_nodes', default=1)
        self.options.declare('specific_heat', default=921, desc='Specific heat in J/kg/K - default 921 for aluminum')

    def setup(self):
        nn_tot = self.options['num_nodes']
        arange = np.arange(0, nn_tot)

        self.add_input('q_in', units='W', shape=(nn_tot,))
        self.add_input('q_out', units='W', shape=(nn_tot,))
        self.add_input('mass', units='kg')
        self.add_output('dTdt', units='K/s', shape=(nn_tot,))

        self.declare_partials(['dTdt'], ['q_in'], rows=arange, cols=arange)
        self.declare_partials(['dTdt'], ['q_out'], rows=arange, cols=arange)
        self.declare_partials(['dTdt'], ['mass'], rows=arange, cols=np.zeros((nn_tot,)))

    def compute(self, inputs, outputs):
        spec_heat = self.options['specific_heat']
        outputs['dTdt'] = (inputs['q_in'] - inputs['q_out']) / inputs['mass'] / spec_heat

    def compute_partials(self, inputs, J):
        nn_tot = self.options['num_nodes']
        spec_heat = self.options['specific_heat']

        J['dTdt','mass'] = - (inputs['q_in'] - inputs['q_out']) / inputs['mass']**2 / spec_heat
        J['dTdt','q_in'] = 1 / inputs['mass'] / spec_heat
        J['dTdt','q_out'] = - 1 / inputs['mass'] / spec_heat

class CoolantReservoirRate(ExplicitComponent):
    """
    Computes dT/dt of a coolant reservoir based on inflow and current temps and flow rate

    Inputs
    ------
    T_in : float
        Coolant stream in (vector, K)
    T_out : float
        Temperature of the reservoir (vector, K)
    mass : float
        Total quantity of coolant (scalar, kg)
    mdot_coolant : float
        Mass flow rate of the coolant (vector, kg/s)

    Outputs
    -------
    dTdt : float
        First derivative of temperature (vector, K/s)

    Options
    -------
    num_nodes : float
        The number of analysis points to run
    """
    def initialize(self):
        self.options.declare('num_nodes', default=1)

    def setup(self):
        nn_tot = self.options['num_nodes']
        arange = np.arange(0, nn_tot)

        self.add_input('T_in', units='K', shape=(nn_tot,))
        self.add_input('T_out', units='K', shape=(nn_tot,))
        self.add_input('mdot_coolant', units='kg/s', shape=(nn_tot,))
        self.add_input('mass', units='kg')
        self.add_output('dTdt', units='K/s', shape=(nn_tot,))

        self.declare_partials(['dTdt'], ['T_in','T_out','mdot_coolant'], rows=arange, cols=arange)
        self.declare_partials(['dTdt'], ['mass'], rows=arange, cols=np.zeros((nn_tot,)))

    def compute(self, inputs, outputs):
        outputs['dTdt'] = inputs['mdot_coolant'] / inputs['mass'] * (inputs['T_in'] - inputs['T_out'])

    def compute_partials(self, inputs, J):
        J['dTdt','mass'] = - inputs['mdot_coolant'] / inputs['mass']**2 * (inputs['T_in'] - inputs['T_out'])
        J['dTdt','mdot_coolant'] = 1 / inputs['mass'] * (inputs['T_in'] - inputs['T_out'])
        J['dTdt','T_in'] = inputs['mdot_coolant'] / inputs['mass']
        J['dTdt','T_out'] = - inputs['mdot_coolant'] / inputs['mass']

class ThermalComponentMassless(ImplicitComponent):
    """
    Computes thermal residual of a component with heating, cooling, and thermal mass

    Inputs
    ------
    q_in : float
        Heat generated by the component (vector, W)
    q_out : float
        Heat to waste stream (vector, W)

    Outputs
    -------
    T_object : float
        Object temperature (vector, K/s)

    Options
    -------
    num_nodes : float
        The number of analysis points to run
    """
    def initialize(self):
        self.options.declare('num_nodes',default=1)

    def setup(self):
        nn_tot = self.options['num_nodes']
        arange = np.arange(0, nn_tot)

        self.add_input('q_in', units='W', shape=(nn_tot,))
        self.add_input('q_out', units='W', shape=(nn_tot,))
        self.add_output('T_object', units='K', shape=(nn_tot,))

        self.declare_partials(['T_object'], ['q_in'], rows=arange, cols=arange, val=np.ones((nn_tot,)))
        self.declare_partials(['T_object'], ['q_out'], rows=arange, cols=arange, val=-np.ones((nn_tot,)))

    def apply_nonlinear(self, inputs, outputs, residuals):
        residuals['T_object'] = inputs['q_in'] - inputs['q_out']

class ConstantSurfaceTemperatureColdPlate_NTU(ExplicitComponent):
    """
    Computes heat rejection to fluid stream of a microchannel cold plate
    with uniform temperature

    Inputs
    ------
    T_in : float
        Coolant inlet temperature (vector, K)
    T_surface : float
        Temperature of the cold plate (vector, K)
    mdot_coolant : float
        Mass flow rate of the coolant (vector, kg/s)
    channel_length : float
        Length of each microchannel (scalar, m)
    channel_width : float
        Width of each microchannel (scalar, m)
    channel_height : float
        Height of each microchannel (scalar, m)
    n_parallel : float
        Number of fluid channels (scalar, dimensionless)

    Outputs
    -------
    q : float
        Heat transfer rate from the plate to the fluid (vector, W)
    T_out : float
        Outlet fluid temperature (vector, K)

    Options
    -------
    num_nodes : float
        The number of analysis points to run
    fluid_rho : float
        Coolant density in kg/m**3 (default 0.997, water)
    fluid_k : float
        Thermal conductivity of the fluid (W/m/K) (default 0.405, glycol/water)
    nusselt : float
        Hydraulic diameter Nusselt number of the coolant in the channels
        (default 7.54 for constant temperature infinite parallel plate)
    specific_heat : float
        Specific heat of the coolant (J/kg/K) (default 3801, glycol/water)
    """
    def initialize(self):
        self.options.declare('num_nodes', default=1, desc='Number of analysis points')
        self.options.declare('fluid_rho', default=997.0, desc='Fluid density in kg/m3')
        self.options.declare('fluid_k', default=0.405, desc='Thermal conductivity of the fluid in W / mK')
        self.options.declare('nusselt', default=7.54, desc='Hydraulic diameter Nusselt number')
        self.options.declare('specific_heat', default=3801, desc='Specific heat in J/kg/K')

    def setup(self):
        nn_tot = self.options['num_nodes']
        arange = np.arange(0, nn_tot)

        self.add_input('T_in', units='K', shape=(nn_tot,))
        self.add_input('T_surface', units='K', shape=(nn_tot,))
        self.add_input('channel_width', units='m')
        self.add_input('channel_height', units='m')
        self.add_input('channel_length', units='m')
        self.add_input('n_parallel')
        self.add_input('mdot_coolant', units='kg/s', shape=(nn_tot,))

        self.add_output('q', units='W', shape=(nn_tot,))
        self.add_output('T_out', units='K', shape=(nn_tot,))

        self.declare_partials(['q','T_out'], ['T_in','T_surface','mdot_coolant'], method='cs')
        self.declare_partials(['q','T_out'], ['channel_width','channel_height','channel_length','n_parallel'], method='cs')

    def compute(self, inputs, outputs):
        Ts = inputs['T_surface']
        Ti = inputs['T_in']

        Cmin = inputs['mdot_coolant'] * self.options['specific_heat']

        #cross_section_area = inputs['channel_width'] * inputs['channel_height'] * inputs['n_parallel']
        #flow_rate = inputs['mdot_coolant'] / self.options['fluid_rho'] / cross_section_area # m/s
        surface_area = 2 * (inputs['channel_width']*inputs['channel_length'] +
                            inputs['channel_height'] * inputs['channel_length']) * inputs['n_parallel']
        d_h = 2 * inputs['channel_width'] * inputs['channel_height'] / (inputs['channel_width'] + inputs['channel_height'])

        # redh = self.options['fluid_rho'] * flow_rate * d_h / 3.39e-3
        h = self.options['nusselt'] * self.options['fluid_k'] / d_h
        ntu = surface_area * h / Cmin
        effectiveness = 1 - np.exp(-ntu)
        outputs['q'] = effectiveness * Cmin * (Ts - Ti)
        outputs['T_out'] = inputs['T_in'] + outputs['q'] / inputs['mdot_coolant'] / self.options['specific_heat']

class LiquidCooledComp(Group):
    """A component (heat producing) with thermal mass
    cooled by a cold plate.

    Inputs
    ------
    q_in : float
        Heat produced by the operating component (vector, W)
    mdot_coolant : float
        Coolant mass flow rate (vector, kg/s)
    T_in : float
        Instantaneous coolant inflow temperature (vector, K)
    mass : float
        Object mass (only required in thermal mass mode) (scalar, kg)
    T_initial : float
        Initial temperature of the cold plate (only required in thermal mass mode) / object (scalar, K)
    duration : float
        Duration of mission segment, only required in unsteady mode
    channel_width : float
        Width of coolant channels (scalar, m)
    channel_height : float
        Height of coolant channels (scalar, m)
    channel_length : float
        Length of coolant channels (scalar, m)
    n_parallel : float
        Number of identical coolant channels (scalar, dimensionless)

    Outputs
    -------
    T_out : float
        Instantaneous coolant outlet temperature (vector, K)
    T: float
        Object temperature (vector, K)

    Options
    -------
    specific_heat_object : float
        Specific heat capacity of the object in J / kg / K (default 921 = aluminum)
    specific_heat_coolant : float
        Specific heat capacity of the coolant in J / kg / K (default 3801, glycol/water)
    num_nodes : int
        Number of analysis points to run
    quasi_steady : bool
        Whether or not to treat the component as having thermal mass
    """

    def initialize(self):
        self.options.declare('specific_heat_object', default=921.0, desc='Specific heat in J/kg/K')
        self.options.declare('specific_heat_coolant', default=3801, desc='Specific heat in J/kg/K')
        self.options.declare('quasi_steady', default=False, desc='Treat the component as quasi-steady or with thermal mass')
        self.options.declare('num_nodes', default=1, desc='Number of quasi-steady points to runs')

    def setup(self):
        nn = self.options['num_nodes']
        quasi_steady = self.options['quasi_steady']
        if not quasi_steady:
            self.add_subsystem('base',
                               ThermalComponentWithMass(specific_heat=self.options['specific_heat_object'],
                                                        num_nodes=nn),
                                                        promotes_inputs=['q_in', 'mass'])
            ode_integ = self.add_subsystem('ode_integ', Integrator(num_nodes=nn, diff_units='s', method='simpson', time_setup='duration'),
                                           promotes_outputs=['*'], promotes_inputs=['*'])
            # TODO lower limit 0
            ode_integ.add_integrand('T', rate_name='dTdt', units='K')
            self.connect('base.dTdt','dTdt')
        else:
            self.add_subsystem('base',
                               ThermalComponentMassless(num_nodes=nn),
                               promotes_inputs=['q_in'],
                               promotes_outputs=['T'])
        self.add_subsystem('hex',
                           ConstantSurfaceTemperatureColdPlate_NTU(num_nodes=nn, specific_heat=self.options['specific_heat_coolant']),
                                                                   promotes_inputs=['T_in', ('T_surface','T'),'n_parallel','channel*','mdot_coolant'],
                                                                   promotes_outputs=['T_out'])
        self.connect('hex.q','base.q_out')

class CoolantReservoir(Group):
    """A reservoir of coolant capable of buffering temperature

    Inputs
    ------
    mdot_coolant : float
        Coolant mass flow rate (vector, kg/s)
    T_in : float
        Coolant inflow temperature (vector, K)
    mass : float
        Object mass (only required in thermal mass mode) (scalar, kg)
    T_initial : float
        Initial temperature of the coolant reservoir(only required in thermal mass mode) / object (scalar, K)
    duration : float
        Time step of each mission segment (one for each segment) (scalar, s)
        If a single segment is provided (by default) this variable will be called just 'dt'
        only required in thermal mass mode

    Outputs
    -------
    T_out : float
        Coolant outlet temperature (vector, K)

    Options
    -------
    num_nodes : int
        Number of analysis points to run
    """

    def initialize(self):
        self.options.declare('num_nodes',default=5)

    def setup(self):
        nn = self.options['num_nodes']
        self.add_subsystem('rate',
                           CoolantReservoirRate(num_nodes=nn),
                           promotes_inputs=['T_in', 'T_out', 'mass', 'mdot_coolant'])

        ode_integ = self.add_subsystem('ode_integ', Integrator(num_nodes=nn, diff_units='s', method='simpson', time_setup='duration'),
                                           promotes_outputs=['*'], promotes_inputs=['*'])
        # TODO lower limit 0
        ode_integ.add_integrand('T_out', rate_name='dTdt', start_name='T_initial', end_name='T_final', units='K')
        self.connect('rate.dTdt','dTdt')

class LiquidCoolantTestGroup(Group):
    """A component (heat producing) with thermal mass
    cooled by a cold plate.
    """

    def initialize(self):
        self.options.declare('num_nodes',default=11)
        self.options.declare('quasi_steady', default=False, desc='Treat the component as quasi-steady or with thermal mass')

    def setup(self):
        quasi_steady = self.options['quasi_steady']
        nn = self.options['num_nodes']

        iv = self.add_subsystem('iv',IndepVarComp(), promotes_outputs=['*'])
        #iv.add_output('q_in', val=10*np.concatenate([np.ones((nn,)),0.5*np.ones((nn,)),0.2*np.ones((nn,))]), units='kW')
        throttle_profile = np.ones((nn,))
        iv.add_output('q_in',val=20*throttle_profile, units='kW')
        #iv.add_output('T_in', val=40*np.ones((nn_tot,)), units='degC')
        iv.add_output('mdot_coolant', val=0.1*np.ones((nn,)), units='kg/s')
        iv.add_output('rho_coolant', val=997*np.ones((nn,)),units='kg/m**3')
        iv.add_output('motor_mass', val=50., units='kg')
        iv.add_output('coolant_mass', val=10., units='kg')
        iv.add_output('T_motor_initial', val=15, units='degC')
        iv.add_output('T_res_initial', val=15.1, units='degC')
        iv.add_output('duration', val=800, units='s')
        iv.add_output('channel_width', val=1, units='mm')
        iv.add_output('channel_height', val=20, units='mm')
        iv.add_output('channel_length', val=0.2, units='m')
        iv.add_output('n_parallel', val=20)
        Ueas = np.ones((nn))*260
        h = np.concatenate([np.linspace(0,35000,nn)])
        iv.add_output('fltcond|Ueas', val=Ueas, units='kn' )
        iv.add_output('fltcond|h', val=h, units='ft')


        self.add_subsystem('atmos',
                           ComputeAtmosphericProperties(num_nodes=nn),
                           promotes_inputs=["fltcond|h",
                                            "fltcond|Ueas"])

        if not quasi_steady:
            lc_promotes = ['q_in',('mass','motor_mass'),'duration','channel_*','n_parallel']
        else:
            lc_promotes = ['q_in','channel_*','n_parallel']

        self.add_subsystem('component',
                           LiquidCooledComp(num_nodes=nn,
                                            quasi_steady=quasi_steady),
                                            promotes_inputs=lc_promotes)
        self.add_subsystem('duct',
                           ImplicitCompressibleDuct(num_nodes=nn))

        self.connect('atmos.fltcond|p','duct.p_inf')
        self.connect('atmos.fltcond|T','duct.T_inf')
        self.connect('atmos.fltcond|Utrue','duct.Utrue')

        self.connect('component.T_out','duct.T_in_hot')
        self.connect('rho_coolant','duct.rho_hot')
        if quasi_steady:
            self.connect('duct.T_out_hot','component.T_in')
            self.connect('mdot_coolant',['component.mdot_coolant','duct.mdot_hot'])
        else:
            self.add_subsystem('reservoir',
                               CoolantReservoir(num_nodes=nn),
                                                promotes_inputs=['duration',('mass','coolant_mass')])
            self.connect('duct.T_out_hot','reservoir.T_in')
            self.connect('reservoir.T_out','component.T_in')
            self.connect('mdot_coolant',['component.mdot_coolant','duct.mdot_hot','reservoir.mdot_coolant'])
            self.connect('T_motor_initial','component.T_initial')
            self.connect('T_res_initial','reservoir.T_initial')




if __name__ == '__main__':
    # run this script from the root openconcept directory like so:
    # python .\openconcept\components\ducts.py
    quasi_steady = False
    nn = 11
    prob = Problem(LiquidCoolantTestGroup(quasi_steady=quasi_steady, num_nodes=nn))
    prob.model.options['assembled_jac_type'] = 'csc'
    prob.model.nonlinear_solver=NewtonSolver(iprint=2)
    prob.model.linear_solver = DirectSolver(assemble_jac=True)
    prob.model.nonlinear_solver.options['solve_subsystems'] = True
    prob.model.nonlinear_solver.options['maxiter'] = 20
    prob.model.nonlinear_solver.options['atol'] = 1e-8
    prob.model.nonlinear_solver.options['rtol'] = 1e-8
    prob.model.nonlinear_solver.linesearch = BoundsEnforceLS(bound_enforcement='scalar',print_bound_enforce=True)
    prob.model.nonlinear_solver.linesearch.options['print_bound_enforce'] = True
    prob.setup(check=True,force_alloc_complex=True)

    prob.run_model()
    #print(prob['duct.inlet.M'])
    print(np.max(prob['component.T']-273.15))
    print(np.max(-prob['duct.force.F_net']))

    # prob.check_partials(method='cs', compact_print=True)

    #prob.model.list_outputs(units=True, print_arrays=True)
    if quasi_steady:
        np.save('quasi_steady',prob['component.T'])

    # prob.run_driver()
    # prob.model.list_inputs(units=True)
    t = np.linspace(0,800,nn)/60

    import matplotlib.pyplot as plt
    plt.figure()
    plt.plot(t, prob['component.T'] - 273.15)
    plt.xlabel('time (min)')
    plt.ylabel('motor temp (C)')
    plt.figure()
    plt.plot(prob['fltcond|h'], prob['component.T'] - 273.15)
    plt.xlabel('altitude (ft)')
    plt.ylabel('motor temp (C)')
    plt.figure()
    plt.plot(t, prob['duct.inlet.M'])
    plt.xlabel('Mach number')
    plt.ylabel('steady state motor temp (C)')
    plt.figure()
    plt.plot(prob['duct.inlet.M'], prob['duct.force.F_net'])
    plt.xlabel('M_inf')
    plt.ylabel('drag N')
    plt.figure()
    plt.plot(prob['duct.inlet.M'], prob['duct.mdot']/prob['atmos.fltcond|rho']/prob.get_val('atmos.fltcond|Utrue',units='m/s')/prob.get_val('duct.area_nozzle',units='m**2'))
    plt.xlabel('M_inf')
    plt.ylabel('mdot / rho / U / A_nozzle')
    plt.figure()
    plt.plot(prob['duct.inlet.M'],prob['duct.nozzle.M'])
    plt.xlabel('M_inf')
    # plt.ylabel('M_nozzle')
    plt.show()
    prob.model.list_outputs(print_arrays=True)