from typing import Dict, Sequence, Tuple, Union

import numpy as np
import torch

from swyft.marginals.mask import BallMask, ComboMask
from swyft.types import Array, PriorConfig
from swyft.utils import array_to_tensor, depth, tensor_to_array


class Prior1d:
    def __init__(self, tag, *args):
        self.tag = tag
        self.args = args
        if tag == "normal":
            loc, scale = args[0], args[1]
            self.prior = torch.distributions.Normal(loc, scale)
        elif tag == "uniform":
            x0, x1 = args[0], args[1]
            self.prior = torch.distributions.Uniform(x0, x1)
        elif tag == "lognormal":
            loc, scale = args[0], args[1]
            self.prior = torch.distributions.LogNormal(loc, scale)
        else:
            raise KeyError("Tag unknown")

    def sample(self, N):
        return tensor_to_array(self.prior.sample((N,)), np.float64)

    def log_prob(self, value):
        return self.prior.log_prob(value).numpy()

    def to_cube(self, value):
        return self.prior.cdf(torch.tensor(value)).numpy()

    def from_cube(self, value):
        return self.prior.icdf(torch.tensor(value)).numpy()

    def state_dict(self):
        return dict(tag=self.tag, args=self.args)

    @classmethod
    def from_state_dict(cls, state_dict):
        return cls(state_dict["tag"], *state_dict["args"])


class Prior:
    """Accomodates the completely factorized prior, log_prob, sampling, and 'volume' calculations."""

    def __init__(self, prior_config: PriorConfig, mask=None):
        self.prior_config = prior_config
        self.mask = mask

        self._setup_priors()

    def params(self):
        return self.prior_config.keys()

    def _setup_priors(self):
        result = {}
        for key, value in self.prior_config.items():
            result[key] = Prior1d(value[0], *value[1:])
        self.priors = result

    def sample(self, N):
        if self.mask is None:
            return self._sample_from_priors(N)
        else:
            samples = self.mask.sample(N)
            return self.from_cube(samples)

    def _sample_from_priors(self, N):
        result = {}
        for key, value in self.priors.items():
            result[key] = tensor_to_array(value.sample(N))
        return result

    def volume(self):
        if self.mask is None:
            return 1.0
        else:
            return self.mask.volume

    def log_prob(self, values, unmasked=False):
        log_prob_unmasked = {}
        for key, value in self.priors.items():
            x = torch.tensor(values[key])
            log_prob_unmasked[key] = value.log_prob(x)
        log_prob_unmasked_sum = sum(log_prob_unmasked.values())

        if self.mask is not None:
            cube_values = self.to_cube(values)
            m = self.mask(cube_values)
            log_prob_sum = np.where(
                m, log_prob_unmasked_sum - np.log(self.mask.volume), -np.inf
            )
        else:
            log_prob_sum = log_prob_unmasked_sum

        if unmasked:
            return log_prob_unmasked
        else:
            return log_prob_sum

    def factorized_log_prob(
        self,
        values: Dict[str, Array],
        targets: Union[str, Sequence[str], Sequence[Tuple[str]]],
        unmasked: bool = False,
    ):
        if depth(targets) == 0:
            targets = [(targets,)]
        elif depth(targets) == 1:
            targets = [tuple(targets)]

        log_prob_unmasked = {}
        for target in targets:
            relevant_log_probs = {key: self.priors[key].log_prob for key in target}
            relevant_params = {key: array_to_tensor(values[key]) for key in target}
            log_prob_unmasked[target] = sum(
                relevant_log_probs[key](relevant_params[key]) for key in target
            )

        if not unmasked and self.mask is not None:
            cube_values = self.to_cube(values)
            m = self.mask(cube_values)
            log_prob = {
                target: np.where(m, logp - np.log(self.mask.volume), -np.inf)
                for target, logp in log_prob_unmasked.items()
            }
        else:
            log_prob = log_prob_unmasked

        return log_prob

    def to_cube(self, X):
        out = {}
        for k, v in self.priors.items():
            out[k] = v.to_cube(X[k])
        return out

    def from_cube(self, values):
        result = {}
        for key, value in values.items():
            result[key] = np.array(self.priors[key].from_cube(value))
        return result

    def state_dict(self):
        mask_dict = None if self.mask is None else self.mask.state_dict()
        return dict(prior_config=self.prior_config, mask=mask_dict)

    @classmethod
    def from_state_dict(cls, state_dict):
        mask = (
            None
            if state_dict["mask"] is None
            else ComboMask.from_state_dict(state_dict["mask"])
        )
        return cls(state_dict["prior_config"], mask=mask)

    def get_masked(self, obs, re, N=10000, th=-7):
        if re is None:
            return self
        pars = self.sample(N)
        masklist = {}
        lnL = re.lnL(obs, pars)
        for k, v in self.to_cube(pars).items():
            mask = lnL[(k,)].max() - lnL[(k,)] < -th
            ind_points = v[mask].reshape(-1, 1)
            masklist[k] = BallMask(ind_points)
        mask = ComboMask(masklist)
        return Prior(self.prior_config, mask=mask)