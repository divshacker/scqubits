# hilbert_space.py
#
# This file is part of scqubits.
#
#    Copyright (c) 2019, Jens Koch and Peter Groszkowski
#    All rights reserved.
#
#    This source code is licensed under the BSD-style license found in the
#    LICENSE file in the root directory of this source tree.
############################################################################

import warnings

import numpy as np
import qutip as qt

from scqubits.core.central_dispatch import (DispatchClient,
                                            CENTRAL_DISPATCH)
from scqubits.core.descriptors import ReadOnlyProperty, WatchedProperty
from scqubits.core.harmonic_osc import Oscillator
from scqubits.core.spec_lookup import SpectrumLookup
from scqubits.core.storage import SpectrumData
from scqubits.settings import IN_IPYTHON, TQDM_KWARGS
from scqubits.utils.spectrum_utils import convert_operator_to_qobj

if IN_IPYTHON:
    from tqdm.notebook import tqdm
else:
    from tqdm import tqdm


class InteractionTerm(DispatchClient):
    """
    Class for specifying a term in the interaction Hamiltonian of a composite Hilbert space, and constructing
    the Hamiltonian in qutip.Qobj format. The expected form of the interaction term is of two possible types:
    1. V = g A B, where A, B are Hermitean operators in two specified subsystems,
    2. V = g A B + h.c., where A, B may be non-Hermitean
    
    Parameters
    ----------
    g_strength: float
        coefficient parametrizing the interaction strength
    hilbertspace: HilbertSpace
        specifies the Hilbert space components
    subsys1, subsys2: QuantumSystem
        the two subsystems involved in the interaction
    op1, op2: str or ndarray
        names of operators in the two subsystems
    add_hc: bool, optional (default=False)
        If set to True, the interaction Hamiltonian is of type 2, and the Hermitean conjugate is added.
    """
    g_strength = WatchedProperty('INTERACTIONTERM_UPDATE')
    subsys1 = WatchedProperty('INTERACTIONTERM_UPDATE')
    subsys2 = WatchedProperty('INTERACTIONTERM_UPDATE')
    op1 = WatchedProperty('INTERACTIONTERM_UPDATE')
    op2 = WatchedProperty('INTERACTIONTERM_UPDATE')

    def __init__(self, g_strength, subsys1, op1, subsys2, op2, add_hc=False, hilbertspace=None):
        if hilbertspace:
            warnings.warn("`hilbertspace` is no longer a parameter for initializing an InteractionTerm object.",
                          FutureWarning)
        self.g_strength = g_strength
        self.subsys1 = subsys1
        self.op1 = op1
        self.subsys2 = subsys2
        self.op2 = op2
        self.add_hc = add_hc


class HilbertSpace(DispatchClient):
    """Class holding information about the full Hilbert space, usually composed of multiple subsystems.
    The class provides methods to turn subsystem operators into operators acting on the full Hilbert space, and
    establishes the interface to qutip. Returned operators are of the `qutip.Qobj` type. The class also provides methods
    for obtaining eigenvalues, absorption and emission spectra as a function of an external parameter.
    """
    osc_subsys_list = ReadOnlyProperty()
    qbt_subsys_list = ReadOnlyProperty()
    lookup = ReadOnlyProperty()
    interaction_list = WatchedProperty('INTERACTIONLIST_UPDATE')

    def __init__(self, subsystem_list, interaction_list=None):
        self._subsystems = tuple(subsystem_list)
        if interaction_list:
            self.interaction_list = tuple(interaction_list)
        else:
            self.interaction_list = None

        self._lookup = None
        self._osc_subsys_list = [(index, subsys) for (index, subsys) in enumerate(self)
                                 if isinstance(subsys, Oscillator)]
        self._qbt_subsys_list = [(index, subsys) for (index, subsys) in enumerate(self)
                                 if not isinstance(subsys, Oscillator)]

        CENTRAL_DISPATCH.register('QUANTUMSYSTEM_UPDATE', self)
        CENTRAL_DISPATCH.register('INTERACTIONTERM_UPDATE', self)
        CENTRAL_DISPATCH.register('INTERACTIONLIST_UPDATE', self)

    def __getitem__(self, index):
        return self._subsystems[index]

    def __str__(self):
        output = '====== HilbertSpace object ======\n'
        for subsystem in self:
            output += '\n' + str(subsystem) + '\n'
        return output

    def index(self, item):
        return self._subsystems.index(item)

    def _get_metadata_dict(self):
        meta_dict = {}
        for index, subsystem in enumerate(self):
            subsys_meta = subsystem._get_metadata_dict()
            renamed_subsys_meta = {}
            for key in subsys_meta.keys():
                renamed_subsys_meta[type(subsystem).__name__ + str(index) + '_' + key] = subsys_meta[key]
            meta_dict.update(renamed_subsys_meta)
        return meta_dict

    def receive(self, event, sender, **kwargs):
        if self.lookup is not None:
            if event == 'QUANTUMSYSTEM_UPDATE' and sender in self:
                self.broadcast('HILBERTSPACE_UPDATE')
                self._lookup._out_of_sync = True
                # print('Lookup table now out of sync')
            elif event == 'INTERACTIONTERM_UPDATE' and sender in self.interaction_list:
                self.broadcast('HILBERTSPACE_UPDATE')
                self._lookup._out_of_sync = True
                # print('Lookup table now out of sync')
            elif event == 'INTERACTIONLIST_UPDATE' and sender is self:
                self.broadcast('HILBERTSPACE_UPDATE')
                self._lookup._out_of_sync = True
                # print('Lookup table now out of sync')

    @property
    def subsystem_dims(self):
        """Returns list of the Hilbert space dimensions of each subsystem

        Returns
        -------
        list of int"""
        return [subsystem.truncated_dim for subsystem in self]

    @property
    def dimension(self):
        """Returns total dimension of joint Hilbert space

        Returns
        -------
        int"""
        return np.prod(np.asarray(self.subsystem_dims))

    @property
    def subsystem_count(self):
        """Returns number of subsystems composing the joint Hilbert space

        Returns
        -------
        int"""
        return len(self._subsystems)

    def generate_lookup(self):
        bare_specdata_list = []
        for index, subsys in enumerate(self):
            evals, evecs = subsys.eigensys(evals_count=subsys.truncated_dim)
            bare_specdata_list.append(SpectrumData(energy_table=[evals], state_table=[evecs],
                                                   system_params=subsys.__dict__))

        evals, evecs = self.eigensys(evals_count=self.dimension)
        dressed_specdata = SpectrumData(energy_table=[evals], state_table=[evecs],
                                        system_params=self._get_metadata_dict())
        self._lookup = SpectrumLookup(self, bare_specdata_list=bare_specdata_list, dressed_specdata=dressed_specdata)

    def eigenvals(self, evals_count=6):
        """Calculates eigenvalues of the full Hamiltonian using `qutip.Qob.eigenenergies()`.

        Parameters
        ----------
        evals_count: int, optional
            number of desired eigenvalues/eigenstates

        Returns
        -------
        eigenvalues: ndarray of float
        """
        hamiltonian_mat = self.hamiltonian()
        return hamiltonian_mat.eigenenergies(eigvals=evals_count)

    def eigensys(self, evals_count):
        """Calculates eigenvalues and eigenvectore of the full Hamiltonian using `qutip.Qob.eigenstates()`.

        Parameters
        ----------
        evals_count: int, optional
            number of desired eigenvalues/eigenstates

        Returns
        -------
        evals: ndarray of float
        evecs: ndarray of Qobj kets
        """
        hamiltonian_mat = self.hamiltonian()
        evals, evecs = hamiltonian_mat.eigenstates(eigvals=evals_count)
        return evals, evecs

    def diag_operator(self, diag_elements, subsystem):
        """For given diagonal elements of a diagonal operator in `subsystem`, return the `Qobj` operator for the
        full Hilbert space (perform wrapping in identities for other subsystems).

        Parameters
        ----------
        diag_elements: ndarray of floats
            diagonal elements of subsystem diagonal operator
        subsystem: object derived from QuantumSystem
            subsystem where diagonal operator is defined

        Returns
        -------
        qutip.Qobj operator

        """
        dim = subsystem.truncated_dim
        index = range(dim)
        diag_matrix = np.zeros((dim, dim), dtype=np.float_)
        diag_matrix[index, index] = diag_elements
        return self.identity_wrap(diag_matrix, subsystem)

    def diag_hamiltonian(self, subsystem, evals=None):
        """Returns a `qutip.Qobj` which has the eigenenergies of the object `subsystem` on the diagonal.

        Parameters
        ----------
        subsystem: object derived from `QuantumSystem`
            Subsystem for which the Hamiltonian is to be provided.
        evals: ndarray, optional
            Eigenenergies can be provided as `evals`; otherwise, they are calculated.

        Returns
        -------
        qutip.Qobj operator
        """
        evals_count = subsystem.truncated_dim
        if evals is None:
            evals = subsystem.eigenvals(evals_count=evals_count)
        diag_qt_op = qt.Qobj(inpt=np.diagflat(evals[0:evals_count]))
        return self.identity_wrap(diag_qt_op, subsystem)

    def identity_wrap(self, operator, subsystem, op_in_eigenbasis=False, evecs=None):
        """Wrap given operator in subspace `subsystem` in identity operators to form full Hilbert-space operator.

        Parameters
        ----------
        operator: ndarray or qutip.Qobj or str
            operator acting in Hilbert space of `subsystem`; if str, then this should be an operator name in
            the subsystem, typically not in eigenbasis
        subsystem: object derived from QuantumSystem
            subsystem where diagonal operator is defined
        op_in_eigenbasis: bool
            whether `operator` is given in the `subsystem` eigenbasis; otherwise, the internal QuantumSystem basis is
            assumed
        evecs: ndarray, optional
            internal QuantumSystem eigenstates, used to convert `operator` into eigenbasis

        Returns
        -------
        qutip.Qobj operator
        """
        subsys_operator = convert_operator_to_qobj(operator, subsystem, op_in_eigenbasis, evecs)
        operator_identitywrap_list = [qt.operators.qeye(the_subsys.truncated_dim) for the_subsys in self]
        subsystem_index = self.get_subsys_index(subsystem)
        operator_identitywrap_list[subsystem_index] = subsys_operator
        return qt.tensor(operator_identitywrap_list)

    def hubbard_operator(self, j, k, subsystem):
        """Hubbard operator :math:`|j\\rangle\\langle k|` for system `subsystem`

        Parameters
        ----------
        j,k: int
            eigenstate indices for Hubbard operator
        subsystem: instance derived from QuantumSystem class
            subsystem in which Hubbard operator acts

        Returns
        -------
        qutip.Qobj operator
        """
        dim = subsystem.truncated_dim
        operator = (qt.states.basis(dim, j) * qt.states.basis(dim, k).dag())
        return self.identity_wrap(operator, subsystem)

    def annihilate(self, subsystem):
        """Annihilation operator a for `subsystem`

        Parameters
        ----------
        subsystem: object derived from QuantumSystem
            specifies subsystem in which annihilation operator acts

        Returns
        -------
        qutip.Qobj operator
        """
        dim = subsystem.truncated_dim
        operator = (qt.destroy(dim))
        return self.identity_wrap(operator, subsystem)

    def get_subsys_index(self, subsys):
        """
        Return the index of the given subsystem in the HilbertSpace.

        Parameters
        ----------
        subsys: QuantumSystem

        Returns
        -------
        int
        """
        return self.index(subsys)

    def bare_hamiltonian(self):
        """
        Returns
        -------
        qutip.Qobj operator
            composite Hamiltonian composed of bare Hamiltonians of subsystems independent of the external parameter
        """
        bare_hamiltonian = 0
        for subsys in self:
            evals = subsys.eigenvals(evals_count=subsys.truncated_dim)
            bare_hamiltonian += self.diag_hamiltonian(subsys, evals)
        return bare_hamiltonian

    def get_bare_hamiltonian(self):
        """Deprecated, use `bare_hamiltonian()` instead."""
        warnings.warn('bare_hamiltonian() is deprecated, use bare_hamiltonian() instead', FutureWarning)
        return self.bare_hamiltonian()

    def hamiltonian(self):
        """

        Returns
        -------
        qutip.qobj
            Hamiltonian of the composite system, including the interaction between components
        """
        return self.bare_hamiltonian() + self.interaction_hamiltonian()

    def get_hamiltonian(self):
        """Deprecated, use `hamiltonian()` instead."""
        return self.hamiltonian()

    def interaction_hamiltonian(self):
        """
        Returns
        -------
        qutip.Qobj operator
            interaction Hamiltonian
        """
        if self.interaction_list is None:
            return 0

        hamiltonian = [self.interactionterm_hamiltonian(term) for term in self.interaction_list]
        return sum(hamiltonian)

    def interactionterm_hamiltonian(self, interactionterm, evecs1=None, evecs2=None):
        interaction_op1 = self.identity_wrap(interactionterm.op1, interactionterm.subsys1, evecs=evecs1)
        interaction_op2 = self.identity_wrap(interactionterm.op2, interactionterm.subsys2, evecs=evecs2)
        hamiltonian = interactionterm.g_strength * interaction_op1 * interaction_op2
        if interactionterm.add_hc:
            return hamiltonian + hamiltonian.conj()
        return hamiltonian

    def get_spectrum_vs_paramvals(self, hamiltonian_func, param_vals, evals_count=10, get_eigenstates=False,
                                  param_name="external_parameter"):
        """Return eigenvalues (and optionally eigenstates) of the full Hamiltonian as a function of a parameter.
        Parameter values are specified as a list or array in `param_vals`. The Hamiltonian `hamiltonian_func`
        must be a function of that particular parameter, and is expected to internally set subsystem parameters.
        If a `filename` string is provided, then eigenvalue data is written to that file.

        Parameters
        ----------
        hamiltonian_func: function of one parameter
            function returning the Hamiltonian in `qutip.Qobj` format
        param_vals: ndarray of floats
            array of parameter values
        evals_count: int, optional
            number of desired energy levels (default value = 10)
        get_eigenstates: bool, optional
            set to true if eigenstates should be returned as well (default value = False)
        param_name: str, optional
            name for the parameter that is varied in `param_vals` (default value = "external_parameter")

        Returns
        -------
        SpectrumData object
        """
        paramvals_count = len(param_vals)

        eigenenergy_table = np.empty((paramvals_count, evals_count))
        if get_eigenstates:
            eigenstates_qobj_table = [0] * paramvals_count
        else:
            eigenstates_qobj_table = None

        for param_index, paramval in tqdm(enumerate(param_vals), total=len(param_vals), **TQDM_KWARGS):
            paramval = param_vals[param_index]
            hamiltonian = hamiltonian_func(paramval)

            if get_eigenstates:
                eigenenergies, eigenstates_qobj = hamiltonian.eigenstates(eigvals=evals_count)
                eigenenergy_table[param_index] = eigenenergies
                eigenstates_qobj_table[param_index] = eigenstates_qobj
            else:
                eigenenergies = hamiltonian.eigenenergies(eigvals=evals_count)
                eigenenergy_table[param_index] = eigenenergies

        spectrumdata = SpectrumData(eigenenergy_table, self.__dict__, param_name, param_vals,
                                    state_table=eigenstates_qobj_table)
        return spectrumdata
