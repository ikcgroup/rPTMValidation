#! /usr/bin/env python3
"""
This module provides functions for processing mass spectra.

"""
from __future__ import annotations

import collections
import operator
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from pepfrag import Ion

from crPTMDetermine import annotate

from .constants import ITRAQ_MASSES


Annotation = collections.namedtuple("Annotation",
                                    ["peak_num", "mass_diff", "ion_pos"])
                                    
                                    
MGF_BLOCK = '''BEGIN IONS
TITLE=Locus:{spec_id}
{charge}PEPMASS={pepmass:.5f}
{rtinseconds}{spec}
END IONS
'''


class Spectrum():
    """
    A class to represent a mass spectrum. The class composes a numpy array to
    store the spectral signals and provides methods for manipulating and
    exploring the mass spectrum.

    """

    __slots__ = ("_peaks", "prec_mz", "charge", "retention_time",)

    def __init__(self, peak_list: Union[np.ndarray, List[List[float]]],
                 prec_mz: float, charge: Optional[int],
                 retention_time: Optional[float] = None):
        """
        Initializes the class.

        Args:
            peak_list (list): A list of lists containing m/z, intensity pairs.
            prec_mz (float): The mass/charge ratio of the spectrum precursor.
            charge (int): The charge state of the spectrum precursor.
            ret_time (float): The retention time for the spectrum.

        """
        self._peaks = (peak_list if isinstance(peak_list, np.ndarray)
                       else np.array(peak_list))
        if self._peaks.shape[0] == 2:
            self._peaks = self._peaks.T
        self.prec_mz = prec_mz
        self.charge = charge
        self.retention_time = retention_time

        # Sort the spectrum by the m/z ratios
        self._mz_sort()

    def __iter__(self):
        """
        Implements the __iter__ method for the Spectrum class, using the
        composed numpy array.

        """
        return self._peaks.__iter__()

    def __getitem__(self, indices: Any) -> np.array:
        """
        Implements the __getitem__ method for the Spectrum class, using the
        composed numpy array.

        """
        return self._peaks[indices]

    def __setitem__(self, key, value):
        """
        Implements the __setitem__ method for the Spectrum class, using the
        composed numpy array.

        """
        self._peaks[key] = value

    def __len__(self) -> int:
        """
        Implements the __len__ method for the Spectrum class, using the
        composed numpy array.

        Returns:
            The length of the Spectrum object as an int.

        """
        return self._peaks.shape[0]

    def __repr__(self) -> str:
        """
        Implements the __repr__ method for the Spectrum class, using the
        composed numpy array.

        Returns:
            The official string representation of the Spectrum object.

        """
        out = {s: getattr(self, s) for s in Spectrum.__slots__}
        return f"<{self.__class__.__name__} {out}>"

    def __str__(self) -> str:
        """
        Implements the __str__ method for the Spectrum class.

        Returns:
            string representation of the Spectrum object.

        """
        out = {
            "peaks": len(self._peaks),
            "prec_mz": self.prec_mz,
            "charge": self.charge
        }
        return f"<{self.__class__.__name__} {out}>"

    def __eq__(self, other: object) -> bool:
        """
        Implements the __eq__ method for the Spectrum class.

        """
        if not isinstance(other, Spectrum):
            raise NotImplementedError()
        return (np.array_equal(self._peaks, other._peaks) and
                (self.prec_mz, self.charge) == (other.prec_mz, other.charge))

    def __nonzero__(self) -> bool:
        """
        Implements the __nonzero__ method for the Spectrum class, testing
        whether the underlying numpy array has been populated.

        """
        return self._peaks.size > 0

    def _mz_sort(self):
        """
        Sorts the spectrum by the m/z ratios.

        """
        self._peaks = self._peaks[self._peaks[:, 0].argsort()]

    @property
    def mz(self) -> np.array:
        """
        Retrieves the mass/charge ratios of the spectrum peaks.

        """
        return self._peaks[:, 0]

    @property
    def intensity(self) -> np.array:
        """
        Retrieves the intensities of the spectrum peaks.

        """
        return self._peaks[:, 1]

    def select(self, peaks: List[int],
               col: Optional[Union[int, List[int]]] = None) -> np.array:
        """
        Extracts only those peak indices in the given list.

        Args:
            peaks (list): A list of peak indices.
            cols (int, optional): The column(s) to retrieve. If None,
                                  retrieve all.

        Returns:
            Current Spectrum object filtered by the given indices.

        """
        return (self._peaks[peaks, :] if col is None
                else self._peaks[peaks, col])

    def normalize(self):
        """
        Normalizes the spectrum to the base peak.

        Returns:
            Spectrum

        """
        self._peaks[:, 1] = self._peaks[:, 1] / self.max_intensity()
        return self

    def max_intensity(self) -> float:
        """
        Finds the maximum intensity in the spectrum.

        Returns:
            The maximum intensity as a float.

        """
        return self.intensity.max()

    def centroid(self):
        """
        Centroids a tandem mass spectrum according to the m/z differences.
        All fragment ions with adjacent m/z differences of less than 0.1 Da
        are centroided into the ion with the highest intensity.

        Returns:
            Centroided Spectrum object.

        """
        if len(self._peaks) <= 1:
            return self

        mz_diffs = np.diff(self.mz)

        centroided = []
        idx = 0
        while idx < len(self._peaks):
            peak = self._peaks[idx]
            if idx >= len(mz_diffs):
                centroided.append(peak)
                break
            diff = mz_diffs[idx]
            if diff > 0.1:
                centroided.append(peak)
            else:
                peak_cluster = [peak]
                _diff = 0
                while _diff <= 0.1:
                    idx += 1
                    peak = self._peaks[idx]
                    peak_cluster.append(peak)
                    if idx == len(mz_diffs):
                        break
                    _diff = mz_diffs[idx]
                if len({p[1] for p in peak_cluster}) == 1:
                    centroided.append(np.array(
                        [sum(p[0] for p in peak_cluster) /
                         float(len(peak_cluster)),
                         peak_cluster[0][1]]))
                else:
                    centroided.append(max(peak_cluster,
                                          key=operator.itemgetter(1)))
            idx += 1

        self._peaks = np.array(centroided)

        return self

    def remove_itraq(self, tol: float = 0.1):
        """
        Removes the iTRAQ fragment peaks from the spectrum.
        https://stackoverflow.com/questions/51744613/numpy-setdiff1d-with-
        tolerance-comparing-a-numpy-array-to-another-and-saving-o.

        Args:
            tol (float, optional): The mass tolerance.

        Returns:
            Spectrum object minus any iTRAQ peaks.

        """
        self._peaks = self._peaks[
            (np.abs(np.subtract.outer(self._peaks[:, 0], ITRAQ_MASSES))
             > tol).all(1)]
        return self

    def annotate(self, theor_ions: List[Ion],
                 tol: float = 0.2) -> Dict[str, Annotation]:
        """
        Annotates the spectrum using the provided theoretical ions.

        Args:
            theor_ions (list): The list of theoretical Ions.
            tol (float, optional): The mass tolerance for annotations.

        Returns:
            A dictionary of ion label to Annotation namedtuple.

        """
        return {k: Annotation(*v)
                for k, v in annotate(list(self._peaks[:, 0]),
                                     theor_ions, tol).items()}

    def denoise(self, assigned_peaks: List[bool],
                max_peaks_per_window: int = 8) -> Tuple[List[int], Spectrum]:
        """
        Denoises the mass spectrum using the annotated ions.

        Args:
            assigned_peaks (list): A list of booleans indicating whether the
                                   corresponding index peak is annotated.
            max_peaks_per_window (int, optional): The maximum number of peaks
                                                  to include per 100 Da window.

        Returns:
            Tuple: The denoised peak indexes as a list, The denoised spectrum

        """
        npeaks = len(self._peaks)
        # Divide the mass spectrum into windows of 100 Da
        n_windows = int((self._peaks[-1][0] - self._peaks[0][0]) / 100.) + 1
        start_idx = 0
        new_peaks: List[int] = []

        for window in range(n_windows):
            # Set up the mass limit for the current window
            max_mass = self._peaks[0][0] + (window + 1) * 100.

            # Find the last index with a peak mass within the current window
            for end_idx in range(start_idx, npeaks):
                if self._peaks[end_idx][0] > max_mass:
                    break

            if end_idx == start_idx:
                if (self._peaks[end_idx][0] <= max_mass and
                        assigned_peaks[end_idx]):
                    new_peaks.append(end_idx)
                continue

            # Sort the peaks within the window in descending order of
            # intensity
            window_peaks = sorted(list(range(start_idx, end_idx)),
                                  key=lambda ii: self._peaks[ii][1],
                                  reverse=True)

            ion_scores = [assigned_peaks[idx] for idx in window_peaks]

            # Sum the scores for the top intensity peaks in the window
            sum_scores = [sum(ion_scores[:idx])
                          for idx in range(1, min(len(ion_scores) + 1,
                                                  max_peaks_per_window + 1))]

            # Take the top number of peaks with the highest number of
            # annotations
            new_peaks += window_peaks[:sum_scores.index(max(sum_scores)) + 1]

            start_idx = end_idx

        return new_peaks, Spectrum(self._peaks[new_peaks, :], self.prec_mz,
                                   self.charge)
                                   
    def to_mgf_block(self, spec_id: str) -> str:
        """
        Constructs a BEGIN IONS - END IONS MGF-format block for the spectrum.

        """
        return MGF_BLOCK.format(
            spec_id=spec_id,
            charge="" if self.charge is None else f"CHARGE={self.charge}+\n",
            pepmass=self.prec_mz,
            rtinseconds="" if self.retention_time is None
            else f"RTINSECONDS={int(self.retention_time)}\n",
            spec="\n".join([f"{mz:.4f} {intensity:.4f} 1"
                            for mz, intensity in self])
        )
