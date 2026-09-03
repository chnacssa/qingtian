"""依赖拓扑排序 — Kahn 算法 + 循环检测

用于 Skill 的安装/卸载顺序编排：
- 安装顺序：先安装依赖，再安装本 Skill（拓扑序正序）
- 卸载顺序：先卸载依赖者，再卸载本 Skill（拓扑序逆序）

版本兼容性：
- semver major 不匹配视为不兼容
- 依赖声明格式: ">=1.0.0", "^2.0.0", "~3.1.0", "1.5.0"
"""

from collections import defaultdict, deque


class DependencyError(Exception):
    """依赖相关异常"""
    pass


class CycleError(DependencyError):
    """循环依赖"""
    pass


class MissingDependencyError(DependencyError):
    """缺失依赖"""
    pass


class VersionConflictError(DependencyError):
    """版本冲突"""
    pass


# ── 版本比较工具 ──


def _parse_semver(version: str) -> tuple[int, int, int]:
    """解析 semver "x.y.z" -> (major, minor, patch)

    忽略 >=, ^, ~ 等前缀。
    """
    cleaned = version.lstrip(">=^~")
    parts = cleaned.split(".", 2)
    try:
        return (
            int(parts[0]) if len(parts) > 0 else 0,
            int(parts[1]) if len(parts) > 1 else 0,
            int(parts[2]) if len(parts) > 2 else 0,
        )
    except (ValueError, IndexError):
        return (0, 0, 0)


def check_version_compatible(installed: str, required: str) -> bool:
    """检查已安装版本是否满足要求

    规则：
    - ">=1.0.0": installed >= 1.0.0
    - "^2.0.0": major 必须为 2
    - "~3.1.0": major.minor 必须为 3.1
    - "1.5.0": 精确匹配 major
    """
    if not installed or not required:
        return True

    inst_major, inst_minor, inst_patch = _parse_semver(installed)
    req_major, req_minor, req_patch = _parse_semver(required)

    if required.startswith(">="):
        # >=x.y.z: installed >= x.y.z
        inst_tuple = (inst_major, inst_minor, inst_patch)
        req_tuple = (req_major, req_minor, req_patch)
        return inst_tuple >= req_tuple
    elif required.startswith("^"):
        # ^x.y.z: major must match
        return inst_major == req_major
    elif required.startswith("~"):
        # ~x.y.z: major.minor must match
        return inst_major == req_major and inst_minor == req_minor
    else:
        # exact or unspecified: major must match
        return inst_major == req_major


# ── 依赖图 ──


class DependencyGraph:
    """Skill 依赖有向图

    用法:
        graph = DependencyGraph()
        graph.add_node("skill_a", version="1.0.0", deps={"skill_b": ">=1.0.0"})
        graph.add_node("skill_b", version="2.0.0")

        # 拓扑排序
        order = graph.topo_sort()  # ["skill_b", "skill_a"]

        # 循环检测
        cycle = graph.detect_cycle()  # 无循环返回 None

        # 加载顺序
        load_order = graph.load_order("skill_a")  # 从根依赖到本 Skill

        # 卸载顺序
        unload_order = graph.unload_order("skill_a")  # 从依赖者到本 Skill
    """

    def __init__(self):
        # {skill_name: {"version": str, "deps": {dep_name: constraint}}}
        self._nodes: dict[str, dict] = {}
        self._adj: dict[str, set[str]] = defaultdict(set)
        self._rev_adj: dict[str, set[str]] = defaultdict(set)

    def add_node(
        self,
        name: str,
        version: str = "1.0.0",
        deps: dict[str, str] | None = None,
    ) -> None:
        """添加节点

        Args:
            name: Skill 名称
            version: 当前版本
            deps: {dep_name: version_constraint} 依赖声明
        """
        self._nodes[name] = {
            "version": version,
            "deps": dict(deps or {}),
        }
        # 更新邻接表
        for dep_name in (deps or {}):
            self._adj[name].add(dep_name)
            self._rev_adj[dep_name].add(name)

    def remove_node(self, name: str) -> None:
        """移除节点"""
        self._nodes.pop(name, None)
        self._adj.pop(name, None)
        for rev_deps in self._rev_adj.values():
            rev_deps.discard(name)
        self._rev_adj.pop(name, None)
        # 也清理其他节点指向本节点的边
        for deps in self._adj.values():
            deps.discard(name)

    def has_node(self, name: str) -> bool:
        """节点是否存在"""
        return name in self._nodes

    def get_dependencies(self, name: str) -> dict[str, str]:
        """获取直接依赖"""
        node = self._nodes.get(name)
        return dict(node["deps"]) if node else {}

    def get_dependents(self, name: str) -> list[str]:
        """获取直接依赖本节点的节点"""
        return list(self._rev_adj.get(name, set()))

    # ── 拓扑排序 ──

    def topo_sort(self) -> list[str]:
        """Kahn 算法拓扑排序

        Returns:
            拓扑序列表（依赖在前）

        Raises:
            CycleError: 检测到循环依赖
        """
        in_degree: dict[str, int] = {}
        for node in self._nodes:
            in_degree[node] = len(self._adj[node])

        queue = deque()
        for node, degree in in_degree.items():
            if degree == 0:
                queue.append(node)

        result = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for dependent in self._rev_adj.get(node, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._nodes):
            cycle_nodes = set(self._nodes.keys()) - set(result)
            raise CycleError(
                f"Circular dependency detected involving: {cycle_nodes}",
            )

        return result

    def detect_cycle(self) -> list[str] | None:
        """检测循环依赖

        Returns:
            循环路径列表（如果有），否则 None
        """
        # Floyd 式 DFS 找环
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self._nodes}
        parent: dict[str, str | None] = {}

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            for dep in self._adj.get(node, set()):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    # 找到环：回溯路径
                    path = [dep, node]
                    curr = node
                    while curr != dep and parent.get(curr) is not None:
                        curr = parent[curr]
                        if curr is not None:
                            path.append(curr)
                    path.reverse()
                    return path
                elif color[dep] == WHITE:
                    parent[dep] = node
                    result = dfs(dep)
                    if result:
                        return result
            color[node] = BLACK
            return None

        for node in self._nodes:
            if color[node] == WHITE:
                result = dfs(node)
                if result:
                    return result
        return None

    # ── 加载/卸载顺序 ──

    def load_order(self, skill_name: str) -> list[str]:
        """计算加载顺序（从根依赖到本 Skill）

        先拓扑排序，然后从排序结果中筛选出本节点的依赖链。
        """
        order = self.topo_sort()
        # 找到本节点在拓扑序中的位置及之前的节点
        result = []
        for node in order:
            result.append(node)
            if node == skill_name:
                break
        return result

    def unload_order(self, skill_name: str) -> list[str]:
        """计算卸载顺序（从依赖者到本 Skill）

        先拓扑排序的反序（先卸载依赖者），然后从本节点开始。
        """
        order = self.topo_sort()
        # 找本节点的位置
        try:
            idx = order.index(skill_name)
        except ValueError:
            return [skill_name]

        # 本节点及之后的节点（依赖本节点的）要反序卸载
        affected = order[idx:]
        return list(reversed(affected))

    # ── 兼容性校验 ──

    def validate(self) -> list[str]:
        """检查所有依赖是否满足

        Returns:
            错误信息列表（空列表表示全部满足）
        """
        errors = []
        for name, node in self._nodes.items():
            for dep_name, constraint in node["deps"].items():
                dep_node = self._nodes.get(dep_name)
                if dep_node is None:
                    errors.append(
                        f"'{name}' depends on '{dep_name}', "
                        f"but '{dep_name}' is not in the graph",
                    )
                    continue
                if not check_version_compatible(dep_node["version"], constraint):
                    errors.append(
                        f"'{name}' requires '{dep_name} {constraint}', "
                        f"but installed version is {dep_node['version']}",
                    )
        return errors


# ── 便利函数 ──


def build_graph(skills: dict[str, dict]) -> DependencyGraph:
    """从技能字典构建依赖图

    Args:
        skills: {skill_name: {"version": str, "deps": {dep_name: constraint}}}

    Returns:
        DependencyGraph 实例
    """
    graph = DependencyGraph()
    for name, info in skills.items():
        graph.add_node(
            name=name,
            version=info.get("version", "1.0.0"),
            deps=info.get("deps", {}),
        )
    return graph


def resolve_load_order(skills: dict[str, dict]) -> list[str]:
    """解析加载顺序（多个 Skill 共同启动时的顺序）

    返回拓扑排序结果。
    """
    graph = build_graph(skills)
    return graph.topo_sort()
