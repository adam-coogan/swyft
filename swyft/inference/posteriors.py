import logging
from warnings import warn

import numpy as np
import torch

import swyft
from swyft.types import Array
from swyft.utils import tupelize_marginals
from .ratios import RatioEstimator
from swyft.networks import DefaultHead, DefaultTail

log = logging.getLogger(__name__)

# TODO: Deprecate!
class PosteriorCollection:
    def __init__(self, rc, prior):
        """Marginal container initialization.

        Args:
            rc (RatioCollection)
            prior (BoundPrior)
        """
        self._rc = rc
        self._prior = prior

    def sample(self, N, obs, device=None, n_batch=10_000):
        """Resturn weighted posterior samples for given observation.

        Args:
            obs (dict): Observation of interest.
            N (int): Number of samples to return.
        """

        v = self._prior.sample(N)  # prior samples

        # Unmasked original wrongly normalized log_prob densities
        # log_probs = self._prior.log_prob(v)
        u = self._prior.ptrans.u(v)

        ratios = self._rc.ratios(
            obs, u, device=device, n_batch=n_batch
        )  # evaluate lnL for reference observation

        weights = {}
        for k, val in ratios.items():

            weights[k] = np.exp(val)

        return dict(params=v, weights=weights)

    def rejection_sample(
        self,
        N: int,
        obs: dict,
        excess_factor: int = 100,
        maxiter: int = 1000,
        device=None,
        n_batch=10_000,
        param_tuples=None,
    ):
        """Samples from each marginal using rejection sampling.

        Args:
            N (int): number of samples in each marginal to output
            obs (dict): target observation
            excess_factor (int, optional): N_to_reject = excess_factor * N . Defaults to 100.
            maxiter (int, optional): maximum loop attempts to draw N. Defaults to 1000.

        Returns:
            Dict[str, np.ndarray]: keys are marginal tuples, values are samples

        Reference:
            Section 23.3.3
            Machine Learning: A Probabilistic Perspective
            Kevin P. Murphy
        """

        weighted_samples = self.sample(
            N=excess_factor * N, obs=obs, device=device, n_batch=10_000
        )

        maximum_log_likelihood_estimates = {
            k: np.log(np.max(v)) for k, v in weighted_samples["weights"].items()
        }

        param_tuples = (
            set(weighted_samples["weights"].keys())
            if param_tuples is None
            else param_tuples
        )
        collector = {k: [] for k in param_tuples}
        out = {}

        # Do the rejection sampling.
        # When a particular key hits the necessary samples, stop calculating on it to reduce cost.
        # Send that key to out.
        counter = 0
        remaining_param_tuples = param_tuples
        while counter < maxiter:
            # Calculate chance to keep a sample

            log_prob_to_keep = {
                pt: np.log(weighted_samples["weights"][pt])
                - maximum_log_likelihood_estimates[pt]
                for pt in remaining_param_tuples
            }

            # Draw and determine if samples are kept
            to_keep = {
                pt: np.less_equal(np.log(np.random.rand(*v.shape)), v)
                for pt, v in log_prob_to_keep.items()
            }

            # Collect samples for every tuple of parameters, if there are enough, add them to out.
            for param_tuple in remaining_param_tuples:
                kept_all_params = weighted_samples["params"][to_keep[param_tuple]]
                kept_params = kept_all_params[..., param_tuple]
                collector[param_tuple].append(kept_params)
                concatenated = np.concatenate(collector[param_tuple])[:N]
                if len(concatenated) == N:
                    out[param_tuple] = concatenated

            # Remove the param_tuples which we already have in out, thus not to calculate for them anymore.
            for param_tuple in out.keys():
                if param_tuple in remaining_param_tuples:
                    remaining_param_tuples.remove(param_tuple)
                    log.debug(f"{len(remaining_param_tuples)} param tuples remaining")

            if len(remaining_param_tuples) > 0:
                weighted_samples = self.sample(
                    N=excess_factor * N, obs=obs, device=device, n_batch=n_batch
                )
            else:
                return out
            counter += 1
        warn(
            f"Max iterations {maxiter} reached there were not enough samples produced in {remaining_param_tuples}."
        )
        return out

    def state_dict(self):
        return dict(rc=self._rc.state_dict(), prior=self._prior.state_dict())

    @classmethod
    def from_state_dict(cls, state_dict):
        return cls(
            RatioCollection.from_state_dict(state_dict["rc"]),
            swyft.Prior.from_state_dict(state_dict["prior"]),
        )

    @classmethod
    def load(cls, filename):
        sd = torch.load(filename)
        return cls.from_state_dict(sd)

    def save(self, filename):
        sd = self.state_dict()
        torch.save(sd, filename)


class Posteriors:
    def __init__(self, dataset, simhook=None):
        # Store relevant information about dataset
        self._prior = dataset.prior
        self._indices = dataset.indices
        self._N = len(dataset)
        self._ratios = {}

        # Temporary
        self._dataset = dataset

    def add(
        self,
        marginals,
        head=DefaultHead,
        tail=DefaultTail,
        head_args: dict = {},
        tail_args: dict = {},
        device="cpu",
    ):
        """Add marginals.

        Args:
            head (swyft.Module instance or type): Head network (optional).
            tail (swyft.Module instance or type): Tail network (optional).
            head_args (dict): Keyword arguments for head network instantiation.
            tail_args (dict): Keyword arguments for tail network instantiation.
        """
        marginals = tupelize_marginals(marginals)
        re = RatioEstimator(
            marginals,
            device=device,
            head=head,
            tail=tail,
            head_args=head_args,
            tail_args=tail_args,
        )
        self._ratios[marginals] = re

    def train(
        self,
        marginals,
        train_args: dict = {}
    ):
        """Train marginals.

        Args:
            train_args (dict): Training keyword arguments.
        """
        marginals = tupelize_marginals(marginals)
        re = self._ratios[marginals]
        re.train(self._dataset, **train_args)

    def sample(self, N, obs0):
        """Resturn weighted posterior samples for given observation.

        Args:
            obs0 (dict): Observation of interest.
            N (int): Number of samples to return.
        """
        v = self._prior.sample(N)  # prior samples

        # Unmasked original wrongly normalized log_prob densities
        #log_probs = self._prior.log_prob(v)
        u = self._prior.ptrans.u(v)

        ratios = self._eval_ratios(obs0, u)  # evaluate lnL for reference observation
        weights = {}
        for k, val in ratios.items():
            weights[k] = np.exp(val)
        return dict(params=v, weights=weights)

#    def sample(self, N, obs, device=None, n_batch=10_000):
#        post = PosteriorCollection(self.ratios, self._prior)
#        samples = post.sample(N, obs, device=device, n_batch=n_batch)
#        return samples

    # TODO: Import from PosteriorCollection
    def rejection_sample(
        self,
        N: int,
        obs: dict,
        excess_factor: int = 100,
        maxiter: int = 1000,
        device=None,
        n_batch=10_000,
    ):
        """Samples from each marginal using rejection sampling.

        Args:
            N (int): number of samples in each marginal to output
            obs (dict): target observation
            excess_factor (int, optional): N_to_reject = excess_factor * N . Defaults to 100.
            maxiter (int, optional): maximum loop attempts to draw N. Defaults to 1000.

        Returns:
            Dict[str, np.ndarray]: keys are marginal tuples, values are samples

        Reference:
            Section 23.3.3
            Machine Learning: A Probabilistic Perspective
            Kevin P. Murphy
        """
        post = PosteriorCollection(self.ratios, self._prior)
        return post.rejection_sample(
            N=N,
            obs=obs,
            excess_factor=excess_factor,
            maxiter=maxiter,
            device=device,
            n_batch=n_batch,
        )

    @property
    def bound(self):
        return self._prior.bound

    @property
    def ptrans(self):
        return self._prior.ptrans

    def _eval_ratios(self, obs: Array, params: Array, n_batch=100):
        result = {}
        for marginals, rc in self._ratios.items():
            ratios = rc.ratios(obs, params, n_batch=n_batch)
            result.update(ratios)
        return result

    def state_dict(self):
        state_dict = dict(
            prior=self._prior.state_dict(),
            indices=self._indices,
            N=self._N,
            ratios=[r.state_dict() for r in self._ratios],
        )
        return state_dict

    @classmethod
    def from_state_dict(cls, state_dict, dataset=None, device="cpu"):
        obj = Posteriors.__new__(Posteriors)
        obj._prior = swyft.Prior.from_state_dict(state_dict["prior"])
        obj._indices = state_dict["indices"]
        obj._N = state_dict["N"]
        obj._ratios = [
            RatioEstimator.from_state_dict(sd) for sd in state_dict["ratios"]
        ]

        obj._dataset = dataset
        obj._device = device
        return obj

    @classmethod
    def load(cls, filename, dataset=None, device="cpu"):
        sd = torch.load(filename)
        return cls.from_state_dict(sd, dataset=dataset, device=device)

    def save(self, filename):
        sd = self.state_dict()
        torch.save(sd, filename)

    @classmethod
    def from_Microscope(cls, micro):
        # FIXME: Return copy
        return micro._posteriors[-1]
