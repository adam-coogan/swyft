import numpy as np
import logging
logging.basicConfig(level=logging.DEBUG, format='%(message)s')

from .cache import DirectoryCache, MemoryCache
from .estimation import Points, RatioEstimator
from .intensity import Prior
from .network import DefaultHead, DefaultTail
from .utils import all_finite, format_param_list, verbosity


class MissingModelError(Exception):
    pass


class Marginals:
    """Marginal container"""

    def __init__(self, ratio, prior):
        """Marginal container initialization.

        Args:
            re (RatioEstimator)
            prior (Prior)
        """
        self._re = ratio
        self._prior = prior

    @property
    def prior(self):
        """(Constrained) prior of the marginal."""
        return self._prior

    @property
    def ratio(self):
        """Ratio estimator for marginals."""
        return self._re

    def __call__(self, obs, n_samples=100000):
        """Return weighted marginal samples.

        Args:
            obs (dict): Observation.
            n_samples (int): Number of samples.

        Returns:
            dict containing samples.

        Note: Observations must be restricted to constrained prior space to
        lead to valid results.
        """
        return self._re.posterior(obs, self._prior, n_samples=n_samples)

    def state_dict(self):
        """Return state_dict."""
        return dict(re=self._re.state_dict(), prior=self._prior.state_dict())

    @classmethod
    def from_state_dict(cls, state_dict):
        """Instantiate Marginals based on state_dict."""
        return Marginals(
            RatioEstimator.from_state_dict(state_dict["re"]),
            Prior.from_state_dict(state_dict["prior"]),
        )

    def gen_constr_prior(self, obs, th=-10):
        """Generate new constrained prior based on ratio estimator and target observation.

        Args:
            obs (dict): Observation.
            th (float): Cutoff maximum log likelihood ratio. Default is -10,
                        which correspond roughly to 4 sigma.

        Returns:
            Prior: Constrained prior.
        """
        return self._prior.get_masked(obs, self._re, th=th)


class NestedRatios:
    """Main SWYFT interface class."""

    def __init__(self, model, prior, obs, noise=None, cache=None, device="cpu"):
        """Initialize swyft.

        Args:
            model (function): Simulator function.
            prior (Prior): Prior model.
            obs (dict): Target observation
            noise (function): Noise model, optional.
            cache (Cache): Storage for simulator results.  If none, create MemoryCache.
            device (str): Device.
        """
        # Not stored
        self._model = model
        self._noise = noise
        if all_finite(obs):
            self._obs = obs
        else:
            raise ValueError("obs must be finite.")
        if cache is None:
            cache = MemoryCache.from_simulator(model, prior)
        self._cache = cache
        self._device = device

        # Stored in state_dict()
        self._converged = False
        self._base_prior = prior  # Initial prior
        self._history = []

    def converged(self):
        return self._converged

    def R(self):
        """Number of rounds."""
        return len(self._history)

    @property
    def obs(self):
        """Reference observation."""
        return self._obs

    @property
    def obs(self):
        """The target observation."""
        return self._obs

    @property
    def marginals(self):
        """Marginals from the last round."""
        if self._history is []:
            if verbosity() >= 1:
                logging.warning("To generated marginals from NRE, call .run(...).")
        return self._history['marginals']

    @property
    def prior(self):
        """Original (unconstrained) prior."""
        return self._base_prior

    def run(
        self,
        Ninit: int = 3000,
        train_args: dict = {},
        head=DefaultHead,
        tail=DefaultTail,
        head_args: dict = {},
        tail_args: dict = {},
        density_factor: float = 2.0,
        volume_conv_th: float = 0.1,
        max_rounds: int = 10,
        Nmax: int = 100000,
        keep_history = False,
    ):
        """Perform 1-dim marginal focus fits.

        Args:
            Ninit (int): Initial number of training points.
            Nmax (int): Maximum number of training points per round.
            density_factor (float > 1): Increase of training point density per round.
            volume_conv_th (float > 0.): Volume convergence threshold.

            train_args (dict): Training keyword arguments.
            head (swyft.Module instance or type): Head network (optional).
            tail (swyft.Module instance or type): Tail network (optional).
            head_args (dict): Keyword arguments for head network instantiation.
            tail_args (dict): Keyword arguments for tail network instantiation.
            max_rounds (int): Maximum number of rounds per invokation of `run`, default 10.
        """

        param_list = self._cache.params
        D = len(param_list)

        assert density_factor > 1.0
        assert volume_conv_th > 0.0
        
        r = 0

        while (not self.converged()) and (r < max_rounds):
            logging.info("NRE round: R = %i"%(self.R()+1))

            if self.R() == 0:  # First round
                prior_R = self._base_prior
                N_R = Ninit
            else:  # Subsequent rounds
                prior_R = self._history[-1]['constr_prior']

                # Derive new number of training points
                prior_Rm1 = self._history[-1]['marginals'].prior
                v_R = prior_R.volume()
                v_Rm1 = prior_Rm1.volume()
                N_Rm1 = self._history[-1]['N']
                density_Rm1 = N_Rm1 / v_Rm1 ** (1 / D)
                density_R = density_factor * density_Rm1
                N_R = min(max(density_R * v_R ** (1 / D), N_Rm1), Nmax)

            logging.info("Number of training samples is N_R = %i"%N_R)

            try:
                marginals_R = self._amortize(
                    prior_R,
                    param_list,
                    head=head,
                    tail=tail,
                    head_args=head_args,
                    tail_args=tail_args,
                    train_args=train_args,
                    N=N_R,
                )
                constr_prior_R = marginals_R.gen_constr_prior(self._obs)
            except MissingModelError:
                logging.info("Missing simulations. Run `cache.simulate(model)`, then re-start `NestedRatios.run`.")
                return

            # Update object history
            self._history.append(dict(
                marginals = marginals_R,
                constr_prior = constr_prior_R,
                N = N_R,
                ))
            r += 1

            # Drop previous marginals
            if (not keep_history) and (self.R() > 1):
                self._history[-2]['marginals'] = None
                self._history[-2]['constr_prior'] = None

            # Check convergence
            logging.debug("constr_prior_R : prior_R volume = %.4g : %.4g"%(
                constr_prior_R.volume(), prior_R.volume()
                ))
            if np.log(prior_R.volume()) - np.log(constr_prior_R.volume()) < volume_conv_th:
                logging.info("Volume converged.")
                self._converged = True

    # NOTE: By convention properties are only quantites that we save in state_dict
    def requires_sim(self):
        """Does cache require simulation runs?"""
        return self._cache.requires_sim

    def gen_1d_marginals(
        self,
        params=None,
        N=1000,
        train_args={},
        head=DefaultHead,
        tail=DefaultTail,
        head_args={},
        tail_args={},
    ):
        """Convenience function to generate 1d marginals."""
        param_list = format_param_list(params, all_params=self._cache.params, mode="1d")
        logging.info("Generating marginals for:", str(param_list))
        return self.gen_custom_marginals(
            param_list,
            N=N,
            train_args=train_args,
            head=head,
            tail=tail,
            head_args=head_args,
            tail_args=tail_args,
        )

    def gen_2d_marginals(
        self,
        params=None,
        N=1000,
        train_args={},
        head=DefaultHead,
        tail=DefaultTail,
        head_args={},
        tail_args={},
    ):
        """Convenience function to generate 2d marginals."""
        param_list = format_param_list(params, all_params=self._cache.params, mode="2d")
        logging.info("Generating marginals for: %s"%str(param_list))
        return self.gen_custom_marginals(
            param_list,
            N=N,
            train_args=train_args,
            head=head,
            tail=tail,
            head_args=head_args,
            tail_args=tail_args,
        )

    def gen_custom_marginals(
        self,
        param_list,
        N=1000,
        train_args={},
        head=DefaultHead,
        tail=DefaultTail,
        head_args={},
        tail_args={},
    ):
        """Perform custom marginal estimation, based on the most recent constrained prior.

        Args:
            param_list (list of tuples of strings): List of parameters for which inference is performed.
            N (int): Number of training points.
            train_args (dict): Training keyword arguments.
            head (swyft.Module instance or type): Head network (optional).
            tail (swyft.Module instance or type): Tail network (optional).
            head_args (dict): Keyword arguments for head network instantiation.
            tail_args (dict): Keyword arguments for tail network instantiation.
        """
        if self.R() == 0:
            prior = self._base_prior
        else:
            prior = self._history[-1]['constr_prior']
            logging.debug("Constrained prior volume = %.4f"%prior.volume())

        param_list = format_param_list(param_list, all_params=self._cache.params)

        marginals = self._amortize(
            prior=prior,
            N=N,
            param_list=param_list,
            head=head,
            tail=tail,
            head_args=head_args,
            tail_args=tail_args,
            train_args=train_args,
        )
        return marginals

    @property
    def cache(self):
        """Return simulation cache."""
        return self._cache

# TODO: Update to handle self._history
#    @property
#    def state_dict(self):
#        """Return `state_dict`."""
#        return dict(
#            base_prior=self._base_prior.state_dict(),
#            obs=self._obs,
#            history=self._history,
#        )
#
#    @classmethod
#    def from_state_dict(cls, state_dict, model, noise=None, cache=None, device="cpu"):
#        """Instantiate NestedRatios from saved `state_dict`."""
#        base_prior = Prior.from_state_dict(state_dict["base_prior"])
#        constr_prior = Prior.from_state_dict(state_dict["constr_prior"])
#        posterior = Marginals.from_state_dict(state_dict["posterior"])
#        obs = state_dict["obs"]
#
#        nr = NestedRatios(
#            model, base_prior, obs, noise=noise, cache=cache, device=device
#        )
#        nr._posterior = posterior
#        nr._constr_prior = constr_prior
#        return nr

    def _amortize(
        self, prior, param_list, N, train_args, head, tail, head_args, tail_args,
    ):
        """Perform amortized inference on constrained priors."""
        self._cache.grow(prior, N)
        if self._cache.requires_sim:
            if self._model is not None:
                self._cache.simulate(self._model)
            else:
                raise MissingModelError("Model not defined.")
        indices = self._cache.sample(prior, N)
        points = Points(indices, self._cache, self._noise)

        if param_list is None:
            param_list = prior.params()

        re = RatioEstimator(
            param_list,
            device=self._device,
            head=head,
            tail=tail,
            tail_args=tail_args,
            head_args=head_args,
        )
        re.train(points, **train_args)

        return Marginals(re, prior)
