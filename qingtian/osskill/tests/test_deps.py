"""依赖拓扑排序单元测试"""

import pytest
from osskill.deps import (
    DependencyGraph,
    CycleError,
    check_version_compatible,
    build_graph,
    resolve_load_order,
)


class TestVersionCompatibility:
    def test_gt(self):
        assert check_version_compatible("1.0.0", ">=1.0.0")
        assert check_version_compatible("2.0.0", ">=1.0.0")
        assert not check_version_compatible("0.9.0", ">=1.0.0")

    def test_caret(self):
        assert check_version_compatible("2.5.0", "^2.0.0")
        assert check_version_compatible("2.0.0", "^2.0.0")
        assert not check_version_compatible("3.0.0", "^2.0.0")
        assert not check_version_compatible("1.9.9", "^2.0.0")

    def test_tilde(self):
        assert check_version_compatible("3.1.5", "~3.1.0")
        assert check_version_compatible("3.1.0", "~3.1.0")
        assert not check_version_compatible("3.2.0", "~3.1.0")
        assert not check_version_compatible("4.1.0", "~3.1.0")

    def test_exact(self):
        assert check_version_compatible("1.5.0", "1.5.0")
        assert check_version_compatible("1.6.0", "1.5.0")  # major match
        assert not check_version_compatible("2.0.0", "1.5.0")  # major mismatch

    def test_empty(self):
        assert check_version_compatible("", ">=1.0.0")
        assert check_version_compatible("1.0.0", "")
        assert check_version_compatible("", "")


class TestDependencyGraph:
    def test_empty_graph(self):
        graph = DependencyGraph()
        assert graph.topo_sort() == []

    def test_single_node(self):
        graph = DependencyGraph()
        graph.add_node("a")
        assert graph.topo_sort() == ["a"]

    def test_simple_deps(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b")
        order = graph.topo_sort()
        assert order == ["b", "a"]

    def test_chain_deps(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b", deps={"c": ">=1.0.0"})
        graph.add_node("c")
        order = graph.topo_sort()
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    def test_diamond_deps(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0", "c": ">=1.0.0"})
        graph.add_node("b", deps={"d": ">=1.0.0"})
        graph.add_node("c", deps={"d": ">=1.0.0"})
        graph.add_node("d")
        order = graph.topo_sort()
        assert order[-1] == "a"  # a depends on everyone
        assert order[0] == "d"   # d depends on no one

    def test_cycle_detection(self):
        graph = DependencyGraph()
        graph.add_node("x", deps={"y": ">=1.0.0"})
        graph.add_node("y", deps={"z": ">=1.0.0"})
        graph.add_node("z", deps={"x": ">=1.0.0"})
        cycle = graph.detect_cycle()
        assert cycle is not None
        assert len(cycle) >= 3

    def test_cycle_error_on_topo_sort(self):
        graph = DependencyGraph()
        graph.add_node("x", deps={"y": ">=1.0.0"})
        graph.add_node("y", deps={"x": ">=1.0.0"})
        with pytest.raises(CycleError):
            graph.topo_sort()

    def test_no_cycle(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b")
        assert graph.detect_cycle() is None

    def test_load_order(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0", "c": ">=1.0.0"})
        graph.add_node("b", deps={"c": ">=1.0.0"})
        graph.add_node("c")
        order = graph.load_order("a")
        assert order[-1] == "a"
        assert "c" in order

    def test_unload_order(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b", deps={"c": ">=1.0.0"})
        graph.add_node("c")
        order = graph.unload_order("a")
        assert order[0] == "a"
        # dependents of a (none) and a itself

    def test_get_dependencies(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        deps = graph.get_dependencies("a")
        assert deps == {"b": ">=1.0.0"}

    def test_get_dependents(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("c", deps={"b": ">=1.0.0"})
        graph.add_node("b")
        assert set(graph.get_dependents("b")) == {"a", "c"}

    def test_remove_node(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b")
        graph.remove_node("b")
        assert not graph.has_node("b")
        # a now has missing dep but topo still works
        assert "a" in graph.topo_sort()

    def test_validate_success(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        graph.add_node("b", version="2.0.0")
        errors = graph.validate()
        assert errors == []

    def test_validate_missing_dep(self):
        graph = DependencyGraph()
        graph.add_node("a", deps={"b": ">=1.0.0"})
        errors = graph.validate()
        assert len(errors) == 1
        assert "depends on" in errors[0]


class TestBuildGraph:
    def test_build_graph_from_dict(self):
        skills = {
            "a": {"version": "1.0.0", "deps": {"b": ">=1.0.0"}},
            "b": {"version": "2.0.0", "deps": {}},
        }
        graph = build_graph(skills)
        assert graph.topo_sort() == ["b", "a"]

    def test_resolve_load_order(self):
        skills = {
            "web": {"version": "1.0.0", "deps": {"db": ">=1.0.0"}},
            "api": {"version": "1.0.0", "deps": {"db": ">=1.0.0"}},
            "db": {"version": "2.0.0", "deps": {}},
        }
        order = resolve_load_order(skills)
        assert order[0] == "db"
        assert set(order) == {"db", "web", "api"}
