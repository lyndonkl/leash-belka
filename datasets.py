import os
import numpy as np
import pandas as pd
import torch
from torch_geometric.data import Dataset, HeteroData
from torch_geometric.loader import DataLoader
from torch_geometric.nn import HeteroConv, GCNConv, Linear, global_mean_pool
from torch.utils.data import random_split
from rdkit import Chem
from rdkit.Chem import AllChem
from Bio.PDB import PDBParser, is_aa
from Bio.PDB.Polypeptide import three_to_index, index_to_one
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, roc_auc_score, precision_score, recall_score, f1_score
from tqdm import tqdm

# Device configuration
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

class MoleculeDataset(Dataset):
    def __init__(self, dataframe, transform=None, pre_transform=None):
        self.dataframe = dataframe.reset_index(drop=True)
        super(MoleculeDataset, self).__init__(None, transform, pre_transform)

    def len(self):
        return len(self.dataframe)

    def get(self, idx):
        row = self.dataframe.iloc[idx]
        smiles = row['molecule_smiles']
        protein_name = row['protein_name']

        if 'binds' in row:
            binds = row['binds']
        else:
            binds = 0

        if 'id' in row:
            molecule_id = row['id']
        else:
            molecule_id = None 

        # Convert SMILES to molecular graph
        mol = Chem.MolFromSmiles(smiles)
        data = HeteroData()
        data['invalid'] = False
        
        if mol is None:
            data['invalid'] = True  # Skip invalid SMILES
            data['dummy_node'].x = torch.zeros((1, 1), dtype=torch.float)  # Dummy node feature
            data['dummy_node'].y = torch.tensor([0], dtype=torch.float)  # Dummy label
            data['dummy_node', 'to', 'dummy_node'].edge_index = torch.tensor([[0], [0]], dtype=torch.long)  # Self-loop edge
            data['dummy_node', 'to', 'dummy_node'].edge_attr = torch.zeros((1, 1), dtype=torch.float)  # Dummy edge attribute
            
            # Update node and edge types
            data.edge_types = [('dummy_node', 'to', 'dummy_node')]
            data.node_types = ['dummy_node'] 
            return data

        atoms_to_remove = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetSymbol() == 'Dy']
        mol = Chem.EditableMol(mol)
        for idx in sorted(atoms_to_remove, reverse=True):
            mol.RemoveAtom(idx)
        mol = mol.GetMol()

        mol = Chem.AddHs(mol)
        # Embed molecule using ETKDG algorithm
        # params = AllChem.ETKDGv3()
        # params.randomSeed = 42
        # embed_result = AllChem.EmbedMolecule(mol, params)
        # if embed_result != 0:
        #     # Embedding failed
        #     print(f"Embedding failed for molecule at index {idx}: {smiles}")
        #     return None

        AllChem.EmbedMolecule(mol, randomSeed=42)

        # # Optimize molecule geometry (optional)
        # optimize_result = AllChem.UFFOptimizeMolecule(mol)
        # if optimize_result != 0:
        #     print(f"Optimization failed for molecule at index {idx}: {smiles}")
        #     # You can choose to skip the molecule or proceed without optimization

        atom_types = [atom.GetSymbol() for atom in mol.GetAtoms()]
        unique_atom_types = list(set(atom_types))

        atom_type_to_indices = {atype: [] for atype in unique_atom_types}
        atom_features = []
        atom_positions = []
        conformer = None

        try:
            conformer = mol.GetConformer()
        except Exception as e:
            print(f"Skipping index {idx} due to error: {e}")

        if conformer is None:
            data['invalid'] = True
            data['dummy_node'].x = torch.zeros((1, 1), dtype=torch.float)  # Dummy node feature
            data['dummy_node'].y = torch.tensor([0], dtype=torch.float)  # Dummy label
            data['dummy_node', 'to', 'dummy_node'].edge_index = torch.tensor([[0], [0]], dtype=torch.long)  # Self-loop edge
            data['dummy_node', 'to', 'dummy_node'].edge_attr = torch.zeros((1, 1), dtype=torch.float)  # Dummy edge attribute
            
            # Update node and edge types
            data.edge_types = [('dummy_node', 'to', 'dummy_node')]
            data.node_types = ['dummy_node'] 
            return data

        for i, atom in enumerate(mol.GetAtoms()):
            atype = atom.GetSymbol()
            atom_type_to_indices[atype].append(i)
            atom_features.append(self.get_atom_features(atom))
            
            pos = mol.GetConformer().GetAtomPosition(i)
            atom_positions.append(np.array([pos.x, pos.y, pos.z], dtype=np.float32))

        # Assign node features and positions per atom type
        for atype in unique_atom_types:
            idx = atom_type_to_indices[atype]
            x = torch.tensor([atom_features[i] for i in idx], dtype=torch.float)
            pos = torch.tensor(np.array([atom_positions[i] for i in idx]), dtype=torch.float)
            data[atype].x = x
            data[atype].pos = pos

        # Precompute mapping from global atom index to local index within atom type
        atom_type_to_local_idx = {
            atype: {global_idx: local_idx for local_idx, global_idx in enumerate(idxs)}
            for atype, idxs in atom_type_to_indices.items()
        }

        # Assign bond edges to specific edge types based on atom types
        bond_edges = {}
        edge_types = set()
        reverse_edge_types = set()
        for bond in mol.GetBonds():
            i = bond.GetBeginAtomIdx()
            j = bond.GetEndAtomIdx()
            atype_i = atom_types[i]
            atype_j = atom_types[j]
            
            # Define edge type in consistent order
            if atype_i <= atype_j:
                # Define edge type
                edge_type = (atype_i, 'bond', atype_j)
                reverse_edge_type = (atype_j, 'bond', atype_i)
                src_atype, tgt_atype = atype_i, atype_j
                src_idx, tgt_idx = i, j
            else:
                # Define edge type
                edge_type = (atype_j, 'bond', atype_i)
                reverse_edge_type = (atype_i, 'bond', atype_j)
                src_atype, tgt_atype = atype_j, atype_i
                src_idx, tgt_idx = j, i
            edge_types.add(edge_type)
            reverse_edge_types.add(reverse_edge_type)

            if edge_type not in bond_edges:
                bond_edges[edge_type] = {'edge_index': [], 'edge_attr': []}

            if reverse_edge_type not in bond_edges:
                bond_edges[reverse_edge_type] = {'edge_index': [], 'edge_attr': []}

            # Retrieve local indices using precomputed mapping
            src_local = atom_type_to_local_idx[src_atype][src_idx]
            tgt_local = atom_type_to_local_idx[tgt_atype][tgt_idx]

            # Append both directions for undirected bonds
            bond_edges[edge_type]['edge_index'].append([src_local, tgt_local])
            bond_edges[reverse_edge_type]['edge_index'].append([tgt_local, src_local])

            # Append bond features for both directions
            bond_feature = self.get_bond_features(bond)
            bond_edges[edge_type]['edge_attr'].append(bond_feature)
            bond_edges[reverse_edge_type]['edge_attr'].append(bond_feature)

        # Assign bond edges to HeteroData
        for edge_type, attrs in bond_edges.items():
            edge_index = torch.tensor(attrs['edge_index'], dtype=torch.long).t().contiguous()
            edge_attr = torch.tensor(attrs['edge_attr'], dtype=torch.float)
            data[edge_type].edge_index = edge_index
            data[edge_type].edge_attr = edge_attr
            

        # Add binding label and metadata
        data['smolecule'].y = torch.tensor([binds], dtype=torch.float)
        data['smolecule'].smiles = smiles
        data['smolecule'].protein_name = protein_name
        data['smolecule'].id = torch.tensor([molecule_id], dtype=torch.long)

        data.node_types = set(unique_atom_types)
        data.edge_types = set(bond_edges.keys())

        return data

    @staticmethod
    def get_atom_features(atom):
        return [
            atom.GetAtomicNum(),
            atom.GetTotalDegree(),
            atom.GetFormalCharge(),
            atom.GetHybridization().real,
            int(atom.GetIsAromatic())
        ]

    @staticmethod
    def get_bond_features(bond):
        bond_type = bond.GetBondType()
        bond_dict = {
            Chem.rdchem.BondType.SINGLE: 0,
            Chem.rdchem.BondType.DOUBLE: 1,
            Chem.rdchem.BondType.TRIPLE: 2,
            Chem.rdchem.BondType.AROMATIC: 3
        }

        return [
            bond_dict.get(bond_type, -1)
        ]


class CombinedDataset(Dataset):
    def __init__(self, dataframe, protein_graphs, transform=None, pre_transform=None, cache_dir='./processed'):
        self.dataframe = dataframe.reset_index(drop=True)
        self.protein_graphs = protein_graphs
        self.cache_dir = cache_dir
        # os.makedirs(self.cache_dir, exist_ok=True)
        super(CombinedDataset, self).__init__(None, transform, pre_transform)

    def len(self):
        return len(self.dataframe)

    def get(self, idx):
        row = self.dataframe.iloc[idx]
        molecule_id = row['id']
        processed_file = os.path.join(self.cache_dir, f'data_{molecule_id}.pt')
        if os.path.exists(processed_file):
            molecule_data, protein_data = torch.load(processed_file)
        else:
            smiles = row['molecule_smiles']
            protein_name = row['protein_name']
            binds = 0

            if 'binds' in row:
                binds = row['binds']

            mol_dataset = MoleculeDataset(pd.DataFrame([row]))
            molecule_data = mol_dataset.get(0)
            if molecule_data is None:
                return None

            protein_data = self.protein_graphs.get(protein_name, HeteroData())

            molecule_data.y = torch.tensor([binds], dtype=torch.float)
            molecule_data.smiles = smiles
            molecule_data.protein_name = protein_name
            molecule_data.id = torch.tensor([molecule_id], dtype=torch.long)

            # torch.save((molecule_data, protein_data), processed_file)

        return molecule_data, protein_data