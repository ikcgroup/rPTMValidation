#! /usr/bin/env python3
"""
Validate PTM identifications derived from shotgun proteomics tandem mass
spectra.

"""
import functools
import itertools
import logging
import os
from typing import (
    Callable,
    Iterable,
    Optional,
    Sequence,
    Set,
)

import cloudpickle
from pepfrag import Peptide

from . import (
    localization,
    readers,
    utilities,
    packing,
    pathway_base,
)
from .constants import RESIDUES
from .peptide_spectrum_match import PSM
from .psm_container import PSMContainer
from .readers import PeptideType
from .results import write_psm_results
from .rptmdetermine_config import DataSetConfig, RPTMDetermineConfig
from .validation_model import ValidationModel


def get_parallel_mods(
        psms: PSMContainer[PSM],
        target_mod: str
) -> Set[str]:
    """Finds all unique modifications present in `psms`."""
    return {
        mod.mod for psm in psms for mod in psm.mods if mod.mod != target_mod
    }


class Validator(pathway_base.PathwayBase):
    """
    The main rPTMDetermine class. The validate method of this class
    encompasses the main functionality of the procedure.

    """
    def __init__(self, config: RPTMDetermineConfig):
        """
        Initialize the Validator object.

        Args:
            config (RPTMDetermineConfig): The RPTMDetermineConfig from JSON.

        """
        super().__init__(config, "validate.log")

        # The PSMContainers are stored in this manner to make it easy to add
        # additional containers while working interactively.
        self.psm_containers = {
            'psms': PSMContainer(),
            'neg_psms': PSMContainer(),
            'unmod_psms': PSMContainer(),
            'decoy_psms': PSMContainer(),
            'neg_unmod_psms': PSMContainer()
        }

        self._container_output_names = {
            'psms': 'Positives',
            'neg_psms': 'Negatives',
            'unmod_psms': 'UnmodifiedPositives',
            'neg_unmod_psms': 'UnmodifiedNegatives',
            'decoy_psms': 'Decoys'
        }

    @property
    def psms(self) -> PSMContainer[PSM]:
        return self.psm_containers['psms']

    @psms.setter
    def psms(self, value: Iterable[PSM]):
        self.psm_containers['psms'] = PSMContainer(value)

    @property
    def neg_psms(self) -> PSMContainer[PSM]:
        return self.psm_containers['neg_psms']

    @neg_psms.setter
    def neg_psms(self, value: Iterable[PSM]):
        self.psm_containers['neg_psms'] = PSMContainer(value)

    @property
    def unmod_psms(self) -> PSMContainer[PSM]:
        return self.psm_containers['unmod_psms']

    @unmod_psms.setter
    def unmod_psms(self, value: Iterable[PSM]):
        self.psm_containers['unmod_psms'] = PSMContainer(value)

    @property
    def decoy_psms(self) -> PSMContainer[PSM]:
        return self.psm_containers['decoy_psms']

    @decoy_psms.setter
    def decoy_psms(self, value: Iterable[PSM]):
        self.psm_containers['decoy_psms'] = PSMContainer(value)

    @property
    def neg_unmod_psms(self) -> PSMContainer[PSM]:
        return self.psm_containers['neg_unmod_psms']

    @neg_unmod_psms.setter
    def neg_unmod_psms(self, value: Iterable[PSM]):
        self.psm_containers['neg_unmod_psms'] = PSMContainer(value)

    ########################
    # Validation
    ########################

    def validate(
            self,
            model_extra_psm_containers: Optional[Sequence[PSMContainer]] = None
    ):
        """
        Validates the identifications in the input data files.

        Args:
            model_extra_psm_containers: Additional PSMContainers containing
                                        modified identifications.

        """
        # Process the input files to extract the modification identifications
        logging.info("Reading database search identifications...")
        self._read_results()
        self._get_mod_identifications()
        allowed_mods = get_parallel_mods(self.psms, self.modification)
        self._get_unmod_identifications(allowed_mods)
        self._get_decoy_identifications(allowed_mods)
        self._process_mass_spectra()

        logging.info('Calculating PSM features...')
        self._calculate_features()

        self._construct_cv_model()

        logging.info('Classifying identifications...')
        for label, psm_container in self.psm_containers.items():
            self._classify(psm_container)
            logging.info(
                f'{len(psm_container.get_validated())} out of '
                f'{len(psm_container)} {label} identifications are validated'
            )

        self._construct_loc_model()

        logging.info('Correcting and localizing modifications...')
        for psm in itertools.chain(
            self.psms, self.neg_psms, *(model_extra_psm_containers or [])
        ):
            localization.correct_and_localize(
                psm,
                self.modification,
                self.mod_mass,
                self.config.target_residue,
                self.loc_model,
                self.model,
                self.model_features,
                self.ptmdb
            )

        logging.info('Writing results to file...')
        self._output_results()

        logging.info('Finished validation.')
        return self.model, self.loc_model

    def _construct_cv_model(self):
        """
        Constructs the machine learning model using cross validation.

        """
        model_cache = os.path.join(
            self.cache_dir, pathway_base.MODEL_CACHE_FILE
        )

        if self.use_cache and os.path.exists(model_cache):
            logging.info('Using cached model...')
            with open(model_cache, 'rb') as fh:
                self.model = cloudpickle.load(fh)
            return

        logging.info('Training machine learning model...')
        self.model = ValidationModel(
            model_features=self.model_features, cv=3,
            n_jobs=self.config.num_cores
        )
        self.model.fit(self.unmod_psms, self.decoy_psms, self.neg_unmod_psms)

        logging.info('Caching trained model...')
        with open(model_cache, 'wb') as fh:
            cloudpickle.dump(self.model, fh)

    def _construct_loc_model(self):
        """
        Constructs the machine learning model without cross validation for
        localization.

        """
        model_cache = os.path.join(
            self.cache_dir, pathway_base.LOCALIZATION_MODEL_CACHE_FILE
        )

        if self.use_cache and os.path.exists(model_cache):
            logging.info('Using cached localization model...')
            with open(model_cache, 'rb') as fh:
                self.loc_model = cloudpickle.load(fh)
            return

        logging.info('Training machine learning model for localization...')
        self.loc_model = ValidationModel(
            model_features=self.model_features, cv=None,
            n_jobs=self.config.num_cores
        )
        self.loc_model.fit(
            self.unmod_psms, self.decoy_psms, self.neg_unmod_psms
        )

        logging.info('Caching trained localization model...')
        with open(model_cache, 'wb') as fh:
            cloudpickle.dump(self.loc_model, fh)

    def _output_results(self):
        """
        Outputs the validation results to CSV format.

        A combined Excel spreadsheet, with each category of PSM in a separate
        sheet, is generated if pandas and openpyxl are available.

        """
        if not os.path.isdir(self.config.output_dir):
            os.makedirs(self.config.output_dir)

        for label, container in self.psm_containers.items():
            # Replace the container label with a more human-readable name
            # if it exists
            label = self._container_output_names.get(label, label)

            output_file = f'{self.file_prefix}{label}.csv'

            write_psm_results(container, output_file)

    def _get_identifications(
            self,
            handler: Callable[[Sequence[readers.SearchResult], str],
                              PSMContainer],
            pos_container_name: str,
            neg_container_name: str
    ):
        """
        Parses the database search results to extract identifications, filtered
        using `handler`.

        Args:
            handler: A function to process the search results from each
                     configured data set. This will be passed, in turn, the
                     positive and negative identifications, as judged by FDR
                     control.
            pos_container_name: The name of the PSMContainer on this class to
                                update with the positive identifications.
            neg_container_name: The name of the PSMContainer on this class to
                                update with the negative identifications.

        """
        for set_id, set_info in self.config.data_sets.items():
            # Apply database search FDR control to the results
            pos_idents, neg_idents = self._split_fdr(
                self.search_results[set_id],
                set_info
            )
            getattr(self, pos_container_name).extend(
                handler(pos_idents, set_id)
            )
            getattr(self, neg_container_name).extend(
                handler(neg_idents, set_id)
            )

        setattr(
            self,
            pos_container_name,
            utilities.deduplicate(getattr(self, pos_container_name))
        )
        setattr(
            self,
            neg_container_name,
            utilities.deduplicate(getattr(self, neg_container_name))
        )

    def _get_mod_identifications(self):
        """

        """
        self._get_identifications(
            self._results_to_mod_psms, 'psms', 'neg_psms'
        )

    def _get_unmod_identifications(self, allowed_mods: Iterable[str]):
        """
        Parses the database search results to extract identifications with
        peptides containing the residues targeted by the modification under
        validation.

        Args:
            allowed_mods: The modifications allowed to exist in the "unmodified"
                          peptides.

        """
        # noinspection PyTypeChecker
        self._get_identifications(
            functools.partial(
                self._results_to_unmod_psms,
                allowed_mods=allowed_mods
            ),
            'unmod_psms',
            'neg_unmod_psms'
        )

    def _get_decoy_identifications(self, allowed_mods: Iterable[str]):
        """

        """
        decoy_psm_cache = os.path.join(
            self.cache_dir, 'decoy_identifications'
        )
        if self.use_cache and os.path.exists(decoy_psm_cache):
            logging.info('Using cached decoy identifications')
            self.decoy_psms = packing.load_from_file(decoy_psm_cache)
            return

        self.decoy_psms = PSMContainer()
        for set_id, set_info in self.config.data_sets.items():
            if set_info.decoy_results is not None:
                logging.info(
                    'Reading decoy identifications from '
                    f'{set_info.decoy_results}'
                )
                self.decoy_psms.extend(
                    self._get_decoys_from_file(set_id, set_info, allowed_mods)
                )
            else:
                # TODO: decoys from self.search_results
                raise NotImplementedError()

        logging.info('Caching decoy identifications')
        packing.save_to_file(self.decoy_psms, decoy_psm_cache)

    def _get_decoys_from_file(
        self,
        data_id: str,
        data_config: DataSetConfig,
        allowed_mods: Iterable[str]
    ) -> PSMContainer[PSM]:
        """
        Reads the decoy identifications from the configured `decoy_results`
        file.

        Args:
            data_id: The data set ID.
            data_config: The data set configuration

        """
        decoy_psms = PSMContainer()
        res_file = os.path.join(data_config.data_dir, data_config.decoy_results)
        for ident in self.decoy_reader.read(
                res_file,
                predicate=lambda r: r.pep_type is readers.PeptideType.decoy
        ):
            if (self._has_target_residue(ident.seq) and
                    all(mod.mod in allowed_mods for mod in ident.mods) and
                    self._valid_peptide_length(ident.seq) and
                    RESIDUES.issuperset(ident.seq)):
                decoy_psms.append(
                    PSM(
                        data_id,
                        ident.spectrum,
                        Peptide(ident.seq, ident.charge, ident.mods),
                        target=False
                    )
                )
        return decoy_psms

    def _results_to_mod_psms(
        self,
        search_res: Iterable[readers.SearchResult],
        data_id: str
    ) -> PSMContainer[PSM]:
        """
        Converts `SearchResult`s to `PSM`s after filtering.

        Filters are applied on peptide type (keep only target identifications),
        identification rank (keep only top-ranking identification), amino acid
        residues (check all valid) and modifications (keep only those with the
        target modification).

        Args:
            search_res: The database search results.
            data_id: The ID of the data set.

        Returns:
            PSMContainer.

        """
        psms: PSMContainer[PSM] = PSMContainer()
        for ident in search_res:
            if (ident.pep_type is readers.PeptideType.decoy or
                    ident.rank != 1 or not RESIDUES.issuperset(ident.seq)):
                # Filter to rank 1, target identifications for validation
                # and ignore placeholder amino acid residue identifications
                continue

            dataset = \
                ident.dataset if ident.dataset is not None else data_id

            if any(ms.mod == self.modification and isinstance(ms.site, int)
                   and ident.seq[ms.site - 1] == self.config.target_residue
                   for ms in ident.mods):
                psms.append(
                    PSM(
                        dataset,
                        ident.spectrum,
                        Peptide(ident.seq, ident.charge, ident.mods)
                    )
                )

        return psms

    def _results_to_unmod_psms(
        self,
        search_res: Iterable[readers.SearchResult],
        data_id: str,
        allowed_mods: Iterable[str]
    ) -> PSMContainer[PSM]:
        """
        Converts `SearchResult`s to `PSM`s after filtering for unmodified PSMs.

        Filters are applied on peptide type (keep only target identifications)
        and amino acid residues (check all valid).

        Args:
            search_res: The database search results.
            data_id: The ID of the data set.
            allowed_mods: The modifications which may be included in
                          "unmodified" peptide identifications.

        Returns:
            PSMContainer.

        """
        psms: PSMContainer[PSM] = PSMContainer()
        for ident in search_res:
            if (not self._has_target_residue(ident.seq) or
                    not RESIDUES.issuperset(ident.seq)):
                continue
            for mod in ident.mods:
                if (mod.mod not in allowed_mods or
                        (isinstance(mod.site, int) and
                         ident.seq[mod.site - 1] ==
                         self.config.target_residue)):
                    break
            else:
                if self._valid_peptide_length(ident.seq):
                    psms.append(
                        PSM(
                            data_id,
                            ident.spectrum,
                            Peptide(ident.seq, ident.charge, ident.mods),
                            target=(ident.pep_type == PeptideType.normal)
                        )
                    )

        return psms

    ########################
    # Utility functions
    ########################

    def _process_mass_spectra(self):
        """
        Processes the input mass spectra to match to their peptides.

        """
        indices = [
            container.get_index(('data_id', 'spec_id'))
            for container in self.psm_containers.values()
        ]

        for data_id, spectra in self.read_mass_spectra():
            for container, index in zip(self.psm_containers.values(), indices):
                for spec_id, spectrum in spectra.items():
                    for psm_idx in index[(data_id, spec_id)]:
                        container[psm_idx].spectrum = spectrum

    def _calculate_features(self):
        """Computes features for all PSMContainers."""
        # def _calculate(psm):
        #     psm.extract_features()
        #     psm.peptide.clean_fragment_ions()
        #
        # Parallel(n_jobs=self.config.num_cores)(
        #     delayed(_calculate)(psm)
        #     for container in self.psm_containers.values() for psm in container
        # )

        for psm_container in self.psm_containers.values():
            for psm in psm_container:
                psm.extract_features()
                psm.peptide.clean_fragment_ions()

    def _filter_psms(self, predicate: Callable[[PSM], bool]):
        """Filters PSMs using the provided `predicate`.

        Args:
            predicate: A function defining a filter condition for the PSMs.

        """
        for psm_container in self.psm_containers.values():
            psm_container[:] = [p for p in psm_container if predicate(p)]
