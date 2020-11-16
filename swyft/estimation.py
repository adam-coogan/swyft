# pylint: disable=no-member, not-callable
from copy import deepcopy
from functools import cached_property

import numpy as np
from scipy.integrate import trapz

import torch
import torch.nn as nn

from .cache import Cache, DataContainer
from .train import get_norms, trainloop
from .network import Network
from .eval import get_ratios, eval_net
from .intensity import construct_intervals, Mask1d, FactorMask, Intensity


class RatioEstimation:
    """
    `RatioEstimation` performs ratio estimation.
    """

    def __init__(
        self, zdim, traindata, combinations=None, head=None, device="cpu", parent=None
    ):
        self.zdim = zdim
        self.head_cls = head  # head network class
        self.device = device
        self.traindata = traindata
        self.parent = parent
        self.net = None
        self.ratio_cache = dict()
        self.combinations = combinations

        self._init_net(self.combinations)

    def _get_dataset(self):
        return self.traindata.get_dataset()

    def _get_net(self, pnum, pdim, head=None, datanorms=None, recycle_net=False):
        # Check whether we can jump-start with using a copy of the previous network
        if self.parent is not None and recycle_net:
            net = deepcopy(self.parent.net)
            return net

        # Otherwise, initialize new neural network
        if self.head_cls is None and head is None:
            head = None
            ds = self._get_dataset()
            ydim = len(ds[0]["x"])
        elif head is not None:
            ydim = head(self.traindata.x0.unsqueeze(0).to(self.device)).shape[1]
            print("Number of output features:", ydim)
        else:
            head = self.head_cls()
            ydim = head(self.traindata.x0.unsqueeze(0)).shape[1]
            print("Number of output features:", ydim)
        net = Network(
            ydim=ydim, pnum=pnum, pdim=pdim, head=head, datanorms=datanorms
        ).to(self.device)
        return net

    def _init_net(self, combinations, recycle_net=False, tag="default"):
        """Generate N-dim posteriors."""
        # Use by default data from last 1-dim round
        dataset = self._get_dataset()
        datanorms = get_norms(dataset, combinations=self.combinations)

        # Generate network
        pnum = len(self.combinations)
        pdim = len(self.combinations[0])

        if recycle_net:
            head = deepcopy(self.net.head)
            net = self._get_net(pnum, pdim, head=head, datanorms=datanorms)
        else:
            net = self._get_net(pnum, pdim, datanorms=datanorms)

        self.net = net

    def train(
        self,
        max_epochs=100,
        nbatch=8,
        lr_schedule=[1e-3, 1e-4, 1e-5],
        nl_schedule=[1.0, 1.0, 1.0],
        early_stopping_patience=1,
        nworkers=0,
        tag="default",
    ):
        """Train higher-dimensional marginal posteriors.

        Args:
            tag (string): Tag indicating network of interest.  Default is "default".
            max_epochs (int): Maximum number of training epochs.
            nbatch (int): Minibatch size.
            lr_schedule (list): List of learning rates.
            early_stopping_patience (int): Early stopping patience.
            nworkers (int): Number of Dataloader workers.
        """
        net = self.net
        dataset = self._get_dataset()

        # Start actual training
        trainloop(
            net,
            dataset,
            combinations=self.combinations,
            device=self.device,
            max_epochs=max_epochs,
            nbatch=nbatch,
            lr_schedule=lr_schedule,
            nl_schedule=nl_schedule,
            early_stopping_patience=early_stopping_patience,
            nworkers=nworkers,
        )

    def _eval_ratios(self, x0, Nmax=1000):
        if x0.tobytes() in self.ratio_cache.keys():
            return
        dataset = self._get_dataset()
        z, ratios = get_ratios(
            torch.tensor(x0).float(),
            self.net,
            dataset,
            device=self.device,
            combinations=self.combinations,
            Nmax=Nmax,
        )
        self.ratio_cache[x0.tobytes()] = [z, ratios]

    def posterior(self, x0, indices, Nmax=1000):
        """Retrieve estimated marginal posterior.

        Args:
            indices (int, list of ints): Parameter indices.
            x0 (array-like): Overwrites target image. Optional.

        Returns:
            x-array, p-array
        """
        self._eval_ratios(x0, Nmax=Nmax)

        if isinstance(indices, int):
            indices = [indices]

        j = self.combinations.index(indices)
        z, ratios = (
            self.ratio_cache[x0.tobytes()][0][:, j],
            self.ratio_cache[x0.tobytes()][1][:, j],
        )

        # 1-dim case
        if len(indices) == 1:
            z = z[:, 0]
            isorted = np.argsort(z)
            z, ratios = z[isorted], ratios[isorted]
            exp_r = np.exp(ratios)
            I = trapz(exp_r, z)
            p = exp_r / I
        else:
            p = np.exp(ratios)
        return z, p

    def load_state(self, PATH):
        self.net.load_state_dict(torch.load(PATH, map_location=self.device))

    def save_state(self, PATH):
        torch.save(self.net.state_dict(), PATH)


class Points(torch.utils.data.Dataset):
    """Points references (observation, parameter) pairs drawn from an inhomogenous Poisson Point Proccess (iP3) Cache.
    Points implements this via a list of indices corresponding to data contained in a cache which is provided at initialization.

    Args:  
        cache (Cache): iP3 cache for zarr storage
        intensity (Intensity): inhomogenous Poisson Point Proccess intensity function on parameters
        noisehook (function): (optional) maps from (x, z) to x with noise
    """
    def __init__(self, cache: Cache, intensity, noisehook=None):
        super().__init__()
        if cache.requires_sim():
            raise RuntimeError("The cache has parameters without a corresponding observation. Try running the simulator.")

        self.cache = cache
        self.intensity = intensity
        self.noisehook = noisehook
        self._indices = None

    def __len__(self):
        assert len(self.indices) <= len(self.cache), "You gave more indices than there are parameter samples in the cache."
        return len(self.indices)
    
    def __getitem__(self, idx):
        i = self.indices[idx]
        x = self.cache.x[i]
        z = self.cache.z[i]

        if self.noisemodel is not None:
            x = self.noisemodel(x, z)

        x = torch.from_numpy(x).float()
        z = torch.from_numpy(z).float()
        return {
            'x': x, 
            'z': z,
        }

    @cached_property
    def indices(self):
        if self._indices is None:
            self._indices = self.cache.sample(self.intensity)
        return self._indices
    
    @classmethod
    def load(cls, path):
        raise NotImplementedError()


# class DataContainer(torch.utils.data.Dataset):
#     """Simple data container class.

#     Note: The noisemodel allows scheduled noise level increase during training.
#     """
#     def __init__(self, cache, indices, noisemodel=None):
#         super().__init__()
#         if cache.requires_sim():
#             raise RuntimeError("The cache has parameters without a corresponding observation. Try running the simulator.")
#         assert len(indices) <= len(cache), "You gave more indices than there are parameter samples in the cache."

#         self.cache = cache
#         self.indices = indices
#         self.noisemodel = noisemodel

#     def __len__(self):
#         return len(self.indices)

#     def __getitem__(self, idx):
#         i = self.indices[idx]
#         x = self.cache.x[i]
#         z = self.cache.z[i]

#         if self.noisemodel is not None:
#             x = self.noisemodel(x, z)

#         x = torch.from_numpy(x).float()
#         z = torch.from_numpy(z).float()

#         xz = dict(x=x, z=z)
#         return xz


# class TrainData:
#     """
#     `TrainData` on contrained priors for Nested Ratio Estimation.

#     Args:
#         x0 (array): Observational data.
#         zdim (int): Number of parameters.
#         head (class): Head network class.
#         noisehook (function): Function return noised data.
#         device (str): Device type.
#     """

#     def __init__(
#         self,
#         x0,
#         zdim,
#         noisehook=None,
#         cache=None,
#         parent=None,
#         nsamples=3000,
#         threshold=1e-7,
#     ):
#         self.x0 = torch.tensor(x0).float()
#         self.zdim = zdim

#         if cache == None:
#             raise ValueError("Need cache!")
#         self.ds = cache

#         self.parent = parent

#         self.intensity = None
#         self.train_indices = None

#         self.noisehook = noisehook

#         self._init_train_data(nsamples=nsamples, threshold=threshold)

#     def get_dataset(self):
#         """Retrieve training dataset from cache and SWYFT object train history."""
#         indices = self.train_indices
#         dataset = DataContainer(self.ds, indices, self.noisehook)
#         return dataset

#     def _init_train_data(self, nsamples=3000, threshold=1e-7):
#         """Advance SWYFT internal training data history on constrained prior."""

#         if self.parent is None:
#             # Generate initial intensity over hypercube
#             mask1d = Mask1d([[0.0, 1.0]])
#             masks_1d = [mask1d] * self.zdim
#         else:
#             # Generate target intensity based on previous round
#             net = self.parent.net
#             intensity = self.parent.traindata.intensity
#             intervals_list = self._get_intervals(net, intensity, threshold=threshold)
#             masks_1d = [Mask1d(tmp) for tmp in intervals_list]

#         factormask = FactorMask(masks_1d)
#         print("Constrained posterior area:", factormask.area())
#         intensity = Intensity(nsamples, factormask)
#         indices = self.ds.sample(intensity)

#         # Append new training samples to train history, including intensity function
#         self.intensity = intensity
#         self.train_indices = indices

#     def _get_intervals(self, net, intensity, N=10000, threshold=1e-7):
#         """Generate intervals from previous posteriors."""
#         z = (
#             torch.tensor(intensity.sample(n=N))
#             .float()
#             .unsqueeze(-1)
#             .to(self.parent.device)
#         )
#         ratios = eval_net(net, self.x0.to(self.parent.device), z)
#         z = z.cpu().numpy()[:, :, 0]
#         ratios = ratios.cpu().numpy()
#         intervals_list = []
#         for i in range(self.zdim):
#             ratios_max = ratios[:, i].max()
#             intervals = construct_intervals(
#                 z[:, i], ratios[:, i] - ratios_max - np.log(threshold)
#             )
#             intervals_list.append(intervals)
#         return intervals_list
