"""
    Utility classes for map_editor.py
"""

import yaml
from topological_node import TopologicalNode


class TopologicalMap(object):

    def __init__(self, filename=None):
        if filename:
            lnodes = self._load_map(filename)
            self.nodes = self._get_nodes(lnodes)
        else:
            self.nodes = []

    def _get_nodes(self, lnodes):
        nodes = []
        for i in lnodes:
            node = TopologicalNode(i['node'])
            nodes.append(node)
        return nodes

    def _load_map(self, filename):
        with open(filename, 'r') as f:
            return yaml.safe_load(f)
        
    def debug(self):
        print type(self.nodes)

    def get_dict(self):
        s = []
        for i in self.nodes:
            node = {}
            node['node'] = {}
            node['node']['pointset'] = 'NA'
            node['node']['name'] = i.name

            node['node']['pose'] = {}
            node['node']['pose']['position'] = {
                'x': i.pose.position.x,
                'y': i.pose.position.y,
                'z': i.pose.position.z
            }
            node['node']['pose']['orientation'] = {
                'w': i.pose.orientation.w,
                'x': i.pose.orientation.x,
                'y': i.pose.orientation.y,
                'z': i.pose.orientation.z
            }

            node['node']['edges'] = []
            for j in i.edges:
                dd = {}
                dd['edge_id'] = j.edge_id
                dd['node'] = j.node
                node['node']['edges'].append(dd)

            s.append(node)
        return s
