#! /usr/bin/env python3
"""
This module provides a class for reading the UniMod database.

"""
import collections
import functools
import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

import lxml.etree as etree

from pepfrag import MassType
from ..constants import ELEMENT_MASSES


MOD_FORMULA_REGEX = re.compile(r"(\w+)\(([0-9]+)\)")


class ModificationNotFoundException(Exception):
    """
    An exception to indicate failure to find a modification in the
    Unimod database.

    """


class PTMDB:
    """
    A class representing the UniMod PTM database.

    """
    formula_regex = re.compile(r"(\w+)\(?([0-9-]+)?\)?")

    def __init__(self, ptm_file: Optional[str] = None):
        """
        Initializes the class object with the database connection and cursor.

        Args:
            ptm_file (str): The path to the Unimod XML file.

        """
        self.ptm_file = (ptm_file if ptm_file is not None
                         else os.path.join(
                            os.path.dirname(os.path.realpath(__file__)),
                            "unimod.xml"))

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        self._initialize()

    def __del__(self):
        """
        Implements the deletion for the class object, commiting any
        outstanding changes and closing the connection to the database.

        """
        self.conn.commit()
        self.conn.close()

    def _initialize(self):
        """
        Constructs the Unimod database using the XML file.

        """
        # Construct modification information table
        self.cursor.execute('''CREATE TABLE mods
                               (mod_id integer PRIMARY KEY, name text,
                                full_name text, mono_mass real, avg_mass real,
                                composition text)''')
        self.cursor.execute("CREATE INDEX name_index ON mods(name)")
        self.cursor.execute(
            "CREATE INDEX full_name_index ON mods(full_name)")

        # Construct table mapping modifications to sites, with a classification
        self.cursor.execute('''CREATE TABLE mod_sites
                               (mod_id integer, site text,
                                classification text)''')
        self.cursor.execute("CREATE INDEX id_index ON mod_sites(mod_id)")

        namespace = "http://www.unimod.org/xmlns/schema/unimod_2"
        for event, element in etree.iterparse(self.ptm_file, events=["end"]):
            if event == "end" and element.tag == f"{{{namespace}}}mod":
                delta = element.find(f"{{{namespace}}}delta")
                mod_id = element.get("record_id")
                self.cursor.execute(
                    '''INSERT INTO mods (mod_id, name, full_name, mono_mass,
                                         avg_mass, composition)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (mod_id,
                     element.get("title"),
                     element.get("full_name").replace(" ", "").lower(),
                     float(delta.get("mono_mass")),
                     float(delta.get("avge_mass")),
                     delta.get("composition")))

                for e in element.findall(f"{{{namespace}}}specificity"):
                    site = e.get("site")
                    classification = e.get("classification")
                    self.cursor.execute(
                        '''INSERT INTO mod_sites (mod_id, site,
                                                  classification)
                           VALUES (?, ?, ?)''',
                        (mod_id, site, classification))

        self.conn.commit()

    def _get_row_by_name(self, name: str):
        """
        Retrieves a row of the database using the name (title) of the entry,
        falling back on a concatenated version of the full name field.

        Args:
            name (str): The name of the modification.

        Returns:

        Raises:
            ModificationNotFoundException.

        """
        self.cursor.execute("SELECT * FROM mods WHERE name=?", (name,))
        res = self.cursor.fetchone()

        if res is not None:
            return res

        self.cursor.execute("SELECT * FROM mods WHERE full_name=?",
                            (name.lower(),))
        res = self.cursor.fetchone()

        if res is not None:
            return res

        raise ModificationNotFoundException(
            f"No modification {name} found in Unimod")

    @functools.lru_cache()
    def get_mass(self, name: str, mass_type: MassType = MassType.mono) \
            -> Optional[float]:
        """
        Retrieves the mass of the specified modification.

        Args:
            name (str): The name of the modification.
            mass_type (MassType, optional): The type of mass to retrieve.

        Returns:
            The mass as a float or None.

        Raises:
            ModificationNotFoundException.

        """
        return self._get_row_by_name(name)[self._get_mass_col(mass_type)]

    @functools.lru_cache()
    def get_by_id(self, ptm_id: int, mass_type: MassType = MassType.mono) \
            -> Optional[Tuple[str, float]]:
        """
        Retrieves a modification entry by ID.

        Args:
            ptm_id (int): The Unimod PTM ID number.
            mass_type (MassType, optional): The type of mass to retrieve.

        Returns:
            A tuple of modification name and mass.

        Raises:
            ModificationNotFoundException.

        """
        self.cursor.execute(
            f"SELECT name, {self._get_mass_col(mass_type)} FROM mods "
            "WHERE mod_id=?", (ptm_id,))

        res = self.cursor.fetchone()

        if res is None:
            raise ModificationNotFoundException(
                f"No modification with ID {ptm_id} found in Unimod")

        return res

    @functools.lru_cache()
    def get_formula(self, name: str) -> Optional[Dict[str, int]]:
        """
        Retrieves the modification formula, in terms of its elemental
        composition.

        Args:
            name (str): The name of the modification.

        Returns:
            A dictionary of element (isotope) to the number of occurrences.

        Raises:
            ModificationNotFoundException.

        """
        comp = self._get_row_by_name(name)["composition"]

        # Parse the composition string
        return {k: int(v) if v else 1
                for k, v in self.formula_regex.findall(comp)}

    @functools.lru_cache()
    def get_name(self, mass: float, mass_type: MassType = MassType.mono,
                 tol: float = 0.001) -> Optional[str]:
        """
        Retrieves the name of the modification, given its mass.

        Args:
            mass (float): The modification mass.
            mass_type (MassType, optional): The mass type.
            tol (float, optional): The mass tolerance for searching.

        Returns:
            The name of the modification as a string, or None.

        Raises:
            ModificationNotFoundException.

        """
        col = self._get_mass_col(mass_type)
        self.cursor.execute(
            f"SELECT name, {col} FROM mods WHERE {col} BETWEEN ? AND ?",
            (mass - tol, mass + tol))
        res = self.cursor.fetchone()

        if res is None:
            raise ModificationNotFoundException(
                f"No modification found with mass within {tol} of {mass}")

        return res[0]

    def get_mods(
        self,
        mass_type: MassType = MassType.mono,
        filter_class: Optional[str] = None) \
            -> Dict[Tuple[str, float], List[str]]:
        """
        Extracts the database entries for the modifications, optionally
        filtering on the classification field.

        Args:
            mass_type (MassType, optional): The mass type.
            filter_class (str, optional): Only extract the modifications which
                                          are classified in this category.

        Returns:
            A dictionary mapping (modification name, modification mass) to
            the peptide sites.

        """
        col = self._get_mass_col(mass_type)
        query = f'''SELECT mods.mod_id, name, {col}, site FROM mods
                    INNER JOIN mod_sites ON mods.mod_id = mod_sites.mod_id'''
        if filter_class is not None:
            query += f' WHERE classification = "{filter_class}"'
        self.cursor.execute(query)

        mods: Dict[Tuple[str, float], List[str]] = \
            collections.defaultdict(list)
        for row in self.cursor.fetchall():
            mods[(row["name"], row[col])].append(row["site"])
        return mods

    def get_ptms(self, mass_type: MassType = MassType.mono) \
            -> Dict[Tuple[str, float], List[str]]:
        """
        Extracts the database entries for those modifications which are
        classified as "Post-translational".

        Args:
            mass_type (MassType, optional): The mass type.

        Returns:
            A dictionary mapping (modification name, modification mass) to
            the peptide sites which can be post-translationally modified.

        """
        return self.get_mods(mass_type=mass_type,
                             filter_class="Post-translational")

    def _get_mass_col(self, mass_type: MassType) -> str:
        """
        Returns the database column for the corresponding mass type.

        Args:
            mass_type (MassType): The type of mass (mono/avg).

        Returns:
            The column name as a string.

        """
        return "mono_mass" if mass_type is MassType.mono else "avg_mass"


def parse_mod_formula(formula: str, mass_type: MassType) -> float:
    """
    Parses the given modification chemical formula to determine the
    associated mass change.

    Args:
        formula (str): The modification chemical formula.
        mass_type (MassType): The mass type to calculate.

    Returns:
        The mass of the modification as a float.

    """
    return sum([getattr(ELEMENT_MASSES[e], mass_type.name) * int(c)
                for e, c in MOD_FORMULA_REGEX.findall(formula)])
