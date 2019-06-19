"""
.. TODO ::
- Overall docstring for this file.
- Docstrings
- Rebuild DMRG and TDVP engines as subclasses of sweep
- Do testing
"""
# Copyright 2018 TeNPy Developers

import numpy as np
import time
import warnings

from ..linalg import np_conserved as npc
from ..networks.mps import MPSEnvironment
from ..networks.mpo import MPOEnvironment
from ..linalg.sparse import NpcLinearOperator
from ..tools.params import get_parameter, unused_parameters


__all__ = ['Sweep', 'EffectiveH', 'OneSiteH', 'TwoSiteH']

class Sweep:
    """Prototype class for a 'sweeping' algorithm.

    In a change from the original setup of the engines, this class is supplied
    with an environment, rather than a state and a model.

    .. todo ::
    Possibly include something like plot_update_stats(), plot_sweep_stats().
    Initialize from psi and model rather than env.
    
    Attributes
    ----------
    eff_H : :class:`~tenpy.algorithms.mps_sweep.EffectiveH`.
        Effective Hamiltonian, used in the local updates.
    EffectiveH : class type
        Class of `eff_H`.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    stats : dict
        Description
    """
    
    def __init__(self, psi, model, EffectiveH, engine_params):
        self.psi = psi
        self.M = model
        self.env = env
        self.EffectiveH = EffectiveH  # class type
        self.engine_params = engine_params
        self.verbose = get_parameter(engine_params, 'verbose', 1, 'Sweep')

        # self.offset_RP = EffectiveH.length - 1
        self.combine = get_parameter(engine_params, 'combine', False, 'Sweep')
        self.finite = self.env.bra.finite
        self.ortho_to_envs = []
        self.mixer = None  # means 'ignore mixer'
        # the mixer is activated in in :meth:`run`.

        self.lanczos_params = get_parameter(engine_params, 'lanczos_params', {}, 'Sweep')
        self.lanczos_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity
        self.trunc_params = get_parameter(engine_params, 'trunc_params', {}, 'Sweep')
        self.trunc_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity

        schedule_i0, update_LP_RP = self.get_sweep_schedule()

        self.init_env(model)

    def __del__(self):
        engine_params = self.engine_params
        unused_parameters(engine_params['lanczos_params'], "Sweep lanczos_params")
        unused_parameters(engine_params['trunc_params'], "Sweep trunc_params")
        if 'mixer_params' in engine_params and engine_params.get('mixer', True):
            unused_parameters(engine_params['mixer_params'], "Sweep mixer_params")
        unused_parameters(engine_params, "Sweep")

    def init_env(self, model=None):
        """(Re-)initialize the environment.

        This function is useful to re-start a Sweep with a slightly different 
        model or different (engine) parameters. Note that we assume that we 
        still have the same `psi`.
        Calls :meth:`reset_stats`.


        Parameters
        ----------
        model : :class:`~tenpy.models.MPOModel`
            The model representing the Hamiltonian for which we want to find the ground state.
            If ``None``, keep the model used before.
        """
        H = model.H_MPO if model is not None else self.env.H
        if self.env is None or self.finite:
            LP = get_parameter(self.engine_params, 'LP', None, 'Sweep')
            RP = get_parameter(self.engine_params, 'RP', None, 'Sweep')
            LP_age = get_parameter(self.engine_params, 'LP_age', 0, 'Sweep')
            RP_age = get_parameter(self.engine_params, 'RP_age', 0, 'Sweep')
        else:  # re-initialize
            compatible = True
            if model is not None:
                try:
                    H.get_W(0).get_leg('wL').test_equal(self.env.H.get_W(0).get_leg('wL'))
                except ValueError:
                    compatible = False
                    warnings.warn("The leg of the new model is incompatible with the previous one."
                                  "Rebuild environment from scratch.")
            if compatible:
                LP = self.env.get_LP(0, False)
                LP_age = self.env.get_LP_age(0)
                RP = self.env.get_RP(self.psi.L - 1, False)
                RP_age = self.env.get_RP_age(self.psi.L - 1)
            else:
                LP = get_parameter(self.engine_params, 'LP', None, 'Sweep')
                RP = get_parameter(self.engine_params, 'RP', None, 'Sweep')
                LP_age = get_parameter(self.engine_params, 'LP_age', 0, 'Sweep')
                RP_age = get_parameter(self.engine_params, 'RP_age', 0, 'Sweep')
            if self.engine_params.get('chi_list', None) is not None:
                warnings.warn("Re-using environment with `chi_list` set! Do you want this?")
        self.env = MPOEnvironment(self.psi, H, self.psi, LP, RP, LP_age, RP_age)

        # (re)initialize ortho_to_envs
        orthogonal_to = get_parameter(self.engine_params, 'orthogonal_to', [], 'Sweep')
        if len(orthogonal_to) > 0:
            if not self.finite:
                raise ValueError("Can't orthogonalize for infinite MPS: overlap not well defined.")
            self.ortho_to_envs = [MPSEnvironment(self.psi, ortho) for ortho in orthogonal_to]

        self.reset_stats()

        # initial sweeps of the environment (without mixer)
        if not self.finite:
            start_env = get_parameter(self.engine_params, 'start_env', 1, 'Sweep')
            self.environment_sweeps(start_env)

    def reset_stats(self):
        """Reset the statistics. Useful if you want to start a new Sweep run.
        
        This method is expected to be overwritten by subclass, and should then
        define self.updat e_stats and self.sweep_stats dicts consistent with the
        statistics generated by the algorithm particular to that subclass.
        """
        warnings.warn("reset_stats() is not overwritten by the engine. No statistics will be collected!")
        self.sweeps = get_parameter(self.engine_params, 'sweep_0', 0, 'Sweep')
        self.shelve = False
        self.chi_list = get_parameter(self.engine_params, 'chi_list', None, 'Sweep')
        if self.chi_list is not None:
            chi_max = self.chi_list[max([k for k in self.chi_list.keys() if k <= self.sweeps])]
            self.trunc_params['chi_max'] = chi_max
            if self.verbose >= 1:
                print("Setting chi_max =", chi_max)
        self.time0 = time.time()

    def environment_sweeps(self, N_sweeps):
        """Perform `N_sweeps` sweeps without bond optimization to update the environment."""
        if N_sweeps <= 0:
            return
        if self.verbose >= 1:
            print("Updating environment")
        for k in range(N_sweeps):
            self.sweep(optimize=False)
            if self.verbose >= 1:
                print('.', end='', flush=True)
        if self.verbose >= 1:
            print("", flush=True)  # end line

    def sweep(self, optimize=True, meas_E_trunc=False):
        """One 'sweep' of a sweeper algorithm.

        Iteratate over the bond which is optimized, to the right and
        then back to the left to the starting point.
        If optimize=False, don't actually diagonalize the effective hamiltonian,
        but only update the environment.

        .. todo ::
        - Remove anything DMRG-specific
        - Make sure all called attributes are actually attributes of the Sweep class.

        Parameters
        ----------
        optimize : bool
            Whether we actually optimize to find the ground state of the effective Hamiltonian.
            (If False, just update the environments).
        **kwargs : dict
            Further parameters given to :meth:`update_local` and :meth:`post_update_local`

        Returns
        -------
        max_trunc_err : float
            Maximal truncation error introduced.
        max_E_trunc : ``None`` | float
            ``None`` if meas_E_trunc is False, else the maximal change of the energy due to the
            truncation.
        """
        self.E_trunc_list = []
        self.trunc_err_list = []
        schedule_i0, update_LP_RP = self.get_sweep_schedule()

        # the actual sweep
        for i0, upd_env in zip(schedule_i0, update_LP_RP):
            update_LP, update_RP = upd_env
            if self.verbose >= 10:
                print("in sweep: i0 =", i0)
            # --------- the main work --------------
            theta, theta_ortho = self.prepare_update(i0)
            update_data = self.update_local(i0, theta, theta_ortho, update_LP, 
                                            update_RP, optimize=optimize)
            if update_LP:
                self.update_LP(i0, update_data['U'])  # (requires updated B)
                for o_env in self.ortho_to_envs:
                    o_env.get_LP(i0 + 1, store=True)
            if update_RP:
                self.update_RP(i0, update_data['VH'])
                for o_env in self.ortho_to_envs:
                    o_env.get_RP(i0, store=True)
            self.post_update_local(i0, update_data, meas_E_trunc, upd_env)

        if optimize:  # count optimization sweeps
            self.sweeps += 1
            if self.chi_list is not None:
                new_chi_max = self.chi_list.get(self.sweeps, None)
                if new_chi_max is not None:
                    self.trunc_params['chi_max'] = new_chi_max
                    if self.verbose >= 1:
                        print("Setting chi_max =", new_chi_max)
            # update mixer
            if self.mixer is not None:
                self.mixer = self.mixer.update_amplitude(self.sweeps)
        if meas_E_trunc:
            return np.max(self.trunc_err_list), np.max(self.E_trunc_list)
        else:
            return np.max(self.trunc_err_list), None

    def get_sweep_schedule(self):
        """Define the schedule of the sweep.

        One 'sweep' is a full sequence from the leftmost site to the right and 
        back. Only those `LP` and `RP` that can be used later should be updated.
        
        Returns
        -------
        schedule_i0 : list
            List of indices of 'active sites'.
        update_LP_RP : list
            List of bools, which indicate whether to update the `LP` and `RP`.
        """
        L = self.env.L
        if self.env.finite:
            schedule_i0 = list(range(0, L - 1)) + list(range(L - 3, 0, -1))
            update_LP_RP = [[True, False]] * (L - 2) + [[False, True]] * (L - 2)
        else:
            assert (L >= 2)
            schedule_i0 = list(range(0, L)) + list(range(L, 0, -1))
            update_LP_RP = [[True, True]] * 2 + [[True, False]] * (L-2) + \
                           [[True, True]] * 2 + [[False, True]] * (L-2)
        return schedule_i0, update_LP_RP

    def get_theta_ortho(self, i0):
        """Get the 2-site wavefunctions to orthogonalize against from :attr:`ortho_to_envs`.

        Parameters
        ----------
        i0 : int
            We want to optimize on sites ``(i0, i0+1)``.

        Returns
        -------
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States to orthogonalize against, with legs 'vL', 'p0', 'p1', 'vR'.
        """
        theta_ortho = []
        for o_env in self.ortho_to_envs:
            theta = o_env.ket.get_theta(i0, n=self.EffectiveH.length)  # the environments are of the form <psi|ortho>
            LP = o_env.get_LP(i0, store=True)
            RP = o_env.get_RP(i0 + self.EffectiveH.length - 1, store=True)
            theta = npc.tensordot(LP, theta, axes=('vR', 'vL'))
            theta = npc.tensordot(theta, RP, axes=('vR', 'vL'))
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
            theta_ortho.append(theta)
        return theta_ortho

    def mixer_cleanup(self):
        """Cleanup the effects of a mixer.

        A :meth:`sweep` with an enabled :class:`Mixer` leaves the MPS `psi` with 2D arrays in `S`.
        To recover the originial form, this function simply performs one sweep with disabled mixer.
        """
        if self.mixer is not None:
            mixer = self.mixer
            self.mixer = None  # disable the mixer
            self.sweep(optimize=False)  # (discard return value)
            self.mixer = mixer  # recover the original mixer

    def mixer_activate(self):
        """Set `self.mixer` to the class specified by `engine_params['mixer']`.

        It is expected that different algorithms have differen ways of implementing
        mixers (with different defaults). Thus, this is algorithm-specific.
        """
        raise NotImplementedError("needs to be overwritten by subclass")

    def prepare_update(self, i0):
        """Prepare everything algorithm-specific to perform a local update."""
        raise NotImplementedError("needs to be overwritten by subclass")

    def update_local(self, i0, theta, **kwargs):
        """Perform algorithm-specific local update."""
        raise NotImplementedError("needs to be overwritten by subclass")

    def post_update_local(self, **kwargs):
        """Algorithm-specific actions to be taken after local update, such as
        collecting statistics.
        """
        raise NotImplementedError("needs to be overwritten by subclass")

    def update_LP(self, i0, U):
        """Update left part of the environment.

        .. todo ::
            figure out size-dependence of combine

        Parameters
        ----------
        i0 : int
            Site index. We calculate ``self.env.get_LP(i0+1)``.
        """
        if self.combine:
            LP = npc.tensordot(self.eff_H.LHeff, U, axes=['(vR.p0*)', '(vL.p0)'])
            LP = npc.tensordot(U.conj(), LP, axes=['(vL*.p0*)', '(vR*.p0)'])
            self.env.set_LP(i0 + 1, LP, age=self.env.get_LP_age(i0) + 1)
        else:  # as implemented directly in the environment
            self.env.get_LP(i0 + 1, store=True)

    def update_RP(self, i0, VH):
        """Update right part of the environment.

        .. todo ::
            figure out size-dependence of combine

        Parameters
        ----------
        i0 : int
            Site index. We calculate ``self.env.get_RP(i0)``.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            The U as returned by SVD, with combined legs, labels ``'vL', '(vR.p1)'``.
        """
        if self.combine:
            RP = npc.tensordot(VH, self.eff_H.RHeff, axes=['(p1.vR)', '(p1*.vL)'])
            RP = npc.tensordot(RP, VH.conj(), axes=['(p1.vL*)', '(p1*.vR*)'])
            self.env.set_RP(i0 + self.EffectiveH.length - 1, RP, age=self.env.get_RP_age(i0 + self.EffectiveH.length - 1) + 1)
        else:  # as implemented directly in the environment
            self.env.get_RP(i0 + self.EffectiveH.length - 1, store=True)


class EffectiveH(NpcLinearOperator):
    """Prototype class for effective Hamiltonians used in sweep algorithms.

    As an example, the effective Hamiltonian for a two-site (DMRG) algorithm 
    looks like:
            |        .---       ---.
            |        |    |   |    |
            |       LP----H0--H1---RP
            |        |    |   |    |
            |        .---       ---.
    where ``H0`` and ``H1`` are MPO tensors.
    
    Attributes
    ----------
    length : int
        Number of (MPS) sites the effective hamiltonian covers.
    """

    # Documentation: This is the local effective Hamiltonian
    # class attribute length
    # provides matvec, __init__ from env, i0
    length = None

    def __init__(self, env, i0):
        raise NotImplementedError("This function should be implemented in derived classes")

    def matvec(self, theta):
        r"""Apply the effective Hamiltonian to `theta`.

        This function turns :class:`EffectiveH` to a linear operator, which can be
        used for :func:`~tenpy.linalg.lanczos.lanczos`. 

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Wave function to apply the effective Hamiltonian to.

        Returns
        -------
        H_theta : :class:`~tenpy.linalg.np_conserved.Array`
            Result of applying the effective Hamiltonian to `theta`, :math:`H |\theta>`.
        """
        raise NotImplementedError("This function should be implemented in derived classes")


class OneSiteH(EffectiveH):
    r"""Class defining the one-site Hamiltonian for Lanczos
    
    The effective one-site Hamiltonian ooks like this:
            |        .---   ---.
            |        |    |    |
            |       LP----W0---RP
            |        |    |    |
            |        .---   ---.
    
    TODO orthogonal theta's?
    
    Parameters
    ----------
    
    Attributes
    ----------
    combine : bool
        Whether to combine legs into pipes. This combines the virtual and 
        physical leg for the left site into pipes. This reduces 
        the overhead of calculating charge combinations in the contractions,
        but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.
        Is originally from the wo-site method; unclear if it works wel for 1 site.
    length : int
        Number of (MPS) sites the effective hamiltonian covers.
    LHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the effective Hamiltonian.
        Labels ``'(vR*.p0)', 'wR', '(vR.p0*)'`` for bra, MPO, ket.
    LP : :class:`tenpy.linalg.np_conserved.Array`
        left part of the environment
    RP : :class:`tenpy.linalg.np_conserved.Array`
        right part of the environment
    W : :class:`tenpy.linalg.np_conserved.Array`
        MPO tensor, applied to the 'p' leg of theta
    """
    length = 1

    def __init__(self, env, i0, combine=False):
        self.LP = env.get_LP(i0)
        self.RP = env.get_RP(i0)
        self.W = env.H.get_W(i0)
        self.combine = combine
        if combine:
            self.combine_Heff()

    def matvec(self, theta):
        """Apply the effective Hamiltonian to `theta`.
        
        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Labels: ``vL, p, vR``

        Returns
        -------
        theta :class:`~tenpy.linalg.np_conserved.Array`
            Product of `theta` and the effective Hamiltonian.
        """
        LP = self.LP
        RP = self.RP
        labels = theta.get_leg_labels()
        if self.combine: 
            theta = theta.combine_legs(['vL', 'p'])  # labels 'vL.p0', 'vR'
            theta = npc.tensordot(self.LHeff, theta, axes=['(vR.p*)', '(vL.p)'])  # labels 'vR*.p0', 'wR', 'vR'
            theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])  # labels 'vR*.p0', 'vL*'
            theta.ireplace_labels(['(vR*.p)', 'vL*'], ['(vL.p)', 'vR'])
        else:
            theta = npc.tensordot(self.LP, theta, axes=['vR', 'vL'])
            theta = npc.tensordot(self.W, theta, axes=[['wL', 'p*'], ['wR', 'p']])
            theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
        theta.itranspose(labels)  # if necessary, transpose
        return theta

    def combine_Heff(self):
        """Combine LP with W.

        .. todo ::
        do we need both LP and RP or can we get away with just one? Is there a
        preference for one or the other?
        """
        LHeff = npc.tensordot(self.LP, self.W, axes=['wR', 'wL'])
        pipeL = LHeff.make_pipe(['vR*', 'p'])
        self.LHeff = LHeff.combine_legs([['vR*', 'p'], ['vR', 'p*']],
                                        pipes=[pipeL, pipeL.conj()],
                                        new_axes=[0, -1])
        RHeff = npc.tensordot(RP, H2, axes=['wL', 'wR'])  #single-site.
        pipeR = RHeff.make_pipe(['p1', 'vL*'])
        self.RHeff = RHeff.combine_legs([['p1', 'vL*'], ['p1*', 'vL']],
                                        pipes=[pipeR, pipeR.conj()],
                                        new_axes=[-1, 0])
        self.pipeL = pipeL
        self.pipeR = pipeR


class TwoSiteH(EffectiveH):
    r"""Class defining the two-site Hamiltonian for Lanczos
    
    The effective two-site Hamiltonian ooks like this:
            |        .---       ---.
            |        |    |   |    |
            |       LP----W0--W1---RP
            |        |    |   |    |
            |        .---       ---.
    
    
    TODO orthogonal theta's.
    
    Attributes
    ----------
    combine : bool
        Whether to combine legs into pipes. This combines the virtual and 
        physical leg for the left site and right site into pipes. This reduces 
        the overhead of calculating charge combinations in the contractions,
        but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.
    length : int
        Number of (MPS) sites the effective hamiltonian covers.
    LHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the effective Hamiltonian.
        Labels ``'(vR*.p0)', 'wR', '(vR.p0*)'`` for bra, MPO, ket.
    RHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the effective Hamiltonian.
        Labels ``'(vL.p1*)', 'wL', '(vL*.p1)'`` for ket, MPO, bra.
    LP : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the environment.
    RP : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the environment
    W1 : :class:`~tenpy.linalg.np_conserved.Array`
        Left MPO tensor, applied to the 'p0' leg of theta
    W2 : :class:`~tenpy.linalg.np_conserved.Array`
        Right MPO tensor, applied to the 'p1' leg of theta
    """
    length = 2

    def __init__(self, env, i0, combine=False):
        self.LP = env.get_LP(i0)
        self.RP = env.get_RP(i0 + 1)
        self.W1 = env.H.get_W(i0).replace_labels(['p', 'p*'], ['p0', 'p0*'])  # 'wL', 'wR', 'p0', 'p0*'
        self.W2 = env.H.get_W(i0 + 1).replace_labels(['p', 'p*'], ['p1', 'p1*'])  # 'wL', 'wR', 'p1', 'p1*'
        self.combine = combine
        if combine:
            self.combine_Heff()

    def matvec(self, theta):
        """Apply the effective Hamiltonian to `theta`.
        
        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Labels: ``vL, p0, p1, vR`` if combine=False, ``vL.p0, p1.vR`` if True
        
        Returns
        -------
        theta :class:`~tenpy.linalg.np_conserved.Array`
            Product of `theta` and the effective Hamiltonian.
        """
        LP = self.LP
        RP = self.RP
        labels = theta.get_leg_labels()
        if self.combine: 
            theta = npc.tensordot(self.LHeff, theta, axes=['(vR.p0*)', '(vL.p0)'])
            theta = npc.tensordot(theta, self.RHeff, axes=[['wR', '(p1.vR)'], ['wL', '(p1*.vL)']])
            theta.ireplace_labels(['(vR*.p0)', '(p1.vL*)'], ['(vL.p0)', '(p1.vR)'])
        else:
            theta = npc.tensordot(self.LP, theta, axes=['vR', 'vL'])
            theta = npc.tensordot(self.W1, theta, axes=[['wL', 'p0*'], ['wR', 'p0']])
            theta = npc.tensordot(theta, self.W2, axes=[['wR', 'p1'], ['wL', 'p1*']])
            theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
        theta.itranspose(labels)  # if necessary, transpose
        return theta

    def combine_Heff(self):
        """Combine LP with W1 and RP with W2 to get the effective parts of the 
        Hamiltonian with piped legs.
        """
        LHeff = npc.tensordot(self.LP, self.W1, axes=['wR', 'wL'])
        pipeL = LHeff.make_pipe(['vR*', 'p0'])
        self.LHeff = LHeff.combine_legs([['vR*', 'p0'], ['vR', 'p0*']],
                                        pipes=[pipeL, pipeL.conj()],
                                        new_axes=[0, -1])
        RHeff = npc.tensordot(self.RP, self.W2, axes=['wL', 'wR'])
        pipeR = RHeff.make_pipe(['p1', 'vL*'])
        self.RHeff = RHeff.combine_legs([['p1', 'vL*'], ['p1*', 'vL']],
                                        pipes=[pipeR, pipeR.conj()],
                                        new_axes=[-1, 0])
        self.pipeL = pipeL
        self.pipeR = pipeR
