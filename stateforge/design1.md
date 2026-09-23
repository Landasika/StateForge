# 创新点一：静态—动态结构反馈机制

## 1. 设计目标与问题定义

现有基于覆盖率的灰盒模糊测试通常把程序执行压缩为一个覆盖集合，例如已经覆盖的行、基本块或边。覆盖集合能够回答“哪些位置被执行过”，却难以回答“从当前执行区域出发，程序结构上哪些位置最值得继续探索”。对于包含多层分支、函数调用和异常处理的 Python 应用而言，单纯的覆盖计数还存在两个限制：第一，它丢失了执行顺序和跨过程控制流关系；第二，它无法区分一个输入是偶然触达了浅层代码，还是已经接近某个尚未覆盖的深层区域。

为此，StateForge 设计了一种建立在 Atheris 之上的静态—动态结构反馈机制（Static-Dynamic Structural Feedback, SDSF）。该机制的核心思想是：首先对目标程序进行静态分析，构建基本块粒度的跨过程控制流图（Interprocedural Control-Flow Graph, ICFG）；随后在 Atheris 执行输入时记录有序运行时轨迹，并将运行时的文件、函数和行号映射回静态 ICFG；最后在静态结构和动态覆盖之间构造 overlay，识别“已覆盖节点到未覆盖节点”的结构前沿（frontier），利用有向 ICFG 距离评价输入与未探索区域的接近程度，并将所得结构分数用于 corpus 优先级排序。

StateForge 不替换 Atheris 的变异算子、执行器或 fuzzing loop。Atheris 继续负责输入变异和目标执行，StateForge 负责解释执行结果并产生结构反馈。因而，本文的创新不在于重新实现一个 Fuzzer，而在于增加一个可复用、可审计且与底层 Fuzzing Engine 解耦的结构反馈层。

给定目标程序 P、初始 corpus C_0 和 fuzzing 预算 B，本文关注如下问题：

> 在不改变 Atheris 的变异和执行机制的前提下，能否利用静态 ICFG 与有序运行时轨迹，为 corpus 中的输入建立结构价值排序，从而更快地到达静态可达但尚未动态覆盖的程序区域？

## 2. 系统模型与总体架构

StateForge 的输入包括目标 Python 项目、初始 corpus 以及 Atheris 产生的运行时输入。静态分析阶段输出一个统一的程序中间表示 ProgramIR，动态阶段输出带有请求边界的运行时事件序列，反馈阶段消费二者并产生覆盖、frontier、距离和 seed 排序结果。整体流程如下：

~~~
目标 Python 项目
        │
        ▼
静态 AST 分析 ──→ 函数级 CFG ──→ 项目级 ICFG（ProgramIR）
                                      │
初始/变异输入 ──→ Atheris ──→ 目标执行 ──→ 有序运行时轨迹
                                      │
                                      ▼
                             运行时—静态映射
                                      │
                                      ▼
                              Static-Dynamic Overlay
                                      │
                         ┌────────────┼────────────┐
                         ▼            ▼            ▼
                       覆盖统计     Frontier      结构距离
                                      │            │
                                      └──────┬─────┘
                                             ▼
                                      Seed/CORPUS 评分
                                             │
                                             ▼
                                  下一轮 corpus 优先级
~~~

该架构要求静态图只构建一次，并由 tracer、mapper、feedback 和 visualization 等模块共享同一份 ProgramIR。因此，同一实验中的所有输入都基于一致的节点和边定义计算覆盖与距离，避免不同模块重复分析造成的图结构不一致。

## 3. 统一程序表示与 ICFG 构建

### 3.1 基本块粒度的程序表示

StateForge 使用四类对象描述静态程序结构：BasicBlock、FlowEdge、FunctionIR 和 ProgramIR。基本块 v ∈ V 至少包含以下属性：

~~~
id, file, module, function, start_line, end_line, kind
~~~

其中，id 采用与源文件、函数和行号相关的稳定命名，而非一次分析过程中临时生成的 b0、b1 等编号。稳定标识使得不同运行、不同实验轮次的动态轨迹可以映射到同一静态节点，也使得覆盖结果能够持久化和复现。

程序级表示定义为：

$$
\mathcal{P} = (V, F, E, M),
$$

其中 V 是基本块集合，F 是函数集合，E 是带类型的控制流边集合，M 保存目标根目录、图版本和未解析调用点等元数据。每个函数 f ∈ F 包含一个入口节点 entry(f)、一个退出节点 exit(f) 以及属于该函数的基本块集合。

### 3.2 函数内控制流

静态分析首先将 Python AST 转换为函数级基本块 CFG。CFG 只描述程序控制流，不引入 HTTP 参数、状态变量、数据依赖或安全语义。当前第一版 ICFG 使用以下五类边：

~~~
FLOW    普通顺序流
TRUE    条件为真时的分支
FALSE   条件为假时的分支
CALL    从调用点进入被调函数
RETURN  从被调函数退出并回到调用点后继
~~~

分支节点仅保存条件或测试语句，分支体中的语句属于对应的后继基本块。这样可以避免将 if admin: delete_user() 中的 delete_user() 错误地计入条件节点。循环结构显式建模回边；continue 指向循环条件，break 指向循环退出位置；return 连接到当前函数的退出节点。该处理保证静态节点的源代码范围与实际执行语义保持一致。

### 3.3 跨过程调用与返回

对每个 AST 调用点，StateForge 首先利用调用者函数、被调用函数名和模块信息进行解析，然后定位包含真实 Call AST 节点的基本块 v_c。若解析得到项目内函数 g，则加入调用边：

$$
e_{call} = (v_c, entry(g), \mathsf{CALL}).
$$

随后，将 exit(g) 连接到调用点的正常后继 v_r：

$$
e_{ret} = (exit(g), v_r, \mathsf{RETURN}).
$$

若调用出现在 return helper() 等没有显式 CFG 后继的位置，则将函数退出节点作为自然返回位置。无法可靠解析的库函数、动态属性分派和项目外调用不会被伪造为内部调用边，而是记录在 ProgramIR.metadata["unresolved_calls"] 中。这种处理宁可暴露静态解析的不确定性，也不把不存在的执行路径加入 ICFG。

因此，项目级 ICFG 定义为：

$$
G = (V, E), \quad E = E_{flow} \cup E_{true} \cup E_{false}
    \cup E_{call} \cup E_{return}.
$$

与将状态关系、数据依赖或语义启发式直接混入控制流图的做法不同，本文的 ICFG 边均表示程序控制流中的可执行关系。后续数据流或语义信息如果需要加入，将作为独立 overlay 处理，而不会改变 ICFG 的路径语义。

## 4. 有序运行时轨迹采集

### 4.1 运行时事件

静态图只能描述潜在的程序路径，因此 StateForge 在目标执行过程中使用 Python tracing API 收集动态事件。每个事件表示为：

$$
\tau_i = (r_i, i, file_i, function_i, line_i, type_i),
$$

其中 r_i 是请求或输入编号，i 是该请求内部的单调序号，type 属于 call、line 或 return。追踪器只保留目标根目录内的事件，并在每个输入之间建立明确的请求边界。

给定输入 x，其运行时轨迹记为：

$$
T(x) = [\tau_0, \tau_1, \ldots, \tau_n].
$$

与只保存 set(file, line) 的覆盖收集方式相比，有序轨迹保留了执行顺序、函数进入和函数退出信息，因此能够恢复函数调用边以及同一请求内的动态相邻关系。不同请求之间不会连接为动态边，避免把两个独立输入的末尾和开头误认为同一条执行路径。

### 4.2 运行时轨迹到基本块轨迹的映射

映射函数记为：

$$
\phi(\tau_i, G) \rightarrow v \in V \cup \{\bot\},
$$

其中 ⊥ 表示无法解析。对于 call 事件，映射器优先定位相应函数的入口节点；对于 return 事件，映射器定位函数退出节点；对于普通 line 事件，映射器根据 (file, function, start_line, end_line) 选择包含该行号的基本块。

将连续映射到同一基本块的事件压缩后，可得到输入的基本块执行序列：

$$
B(x) = [v_0, v_1, \ldots, v_k].
$$

对相邻块 v_j 和 v_{j+1}，映射器记录动态边 (v_j, v_{j+1}) 及其命中的静态边类型。若该相邻关系不存在于 ICFG，则保留为未解析动态关系，而不将其强行计入静态边覆盖。该设计使得静态 CFG 过度近似、异常路径或追踪粒度不足等问题可以被显式观测。

## 5. Static-Dynamic Overlay 与覆盖模型

对于一组运行时轨迹，StateForge 构造静态—动态 overlay：

$$
O(G, T) = (G, C_V, C_E, H_V, H_E),
$$

其中：

* C_V ⊆ V 是动态覆盖的基本块集合；
* C_E ⊆ E 是动态覆盖的 ICFG 边集合；
* H_V(v) 是基本块 v 的命中次数；
* H_E(e) 是边 e 的命中次数。

在此基础上，StateForge 分别报告基本块覆盖、ICFG 边覆盖和调用边覆盖：

$$
Coverage_{BB} = \frac{|C_V|}{|V|},
$$

$$
Coverage_{Edge} = \frac{|C_E|}{|E|},
$$

$$
Coverage_{Call} = \frac{|C_E \cap E_{call}|}{|E_{call}|}.
$$

三种指标分别反映节点、控制流转移和跨函数调用的探索程度。行覆盖可以作为辅助统计，但不作为本文结构反馈的主要语义单位。

## 6. 结构前沿检测

### 6.1 Frontier 定义

StateForge 将已覆盖节点通向未覆盖节点的静态边定义为结构前沿：

$$
Frontier(O) = \{(u,v,e) \mid e=(u,v)\in E,\ u\in C_V,\ v\notin C_V\}.
$$

直观地说，frontier 表示 fuzzing 已经到达的位置与尚未到达区域之间的边界。例如，若分支节点 u 的 true 后继已经执行，而 false 后继 v 尚未执行，则 (u,v) 是一个可操作的 frontier。该定义不依赖特定的 HTTP 参数、关键词或人工规则，只使用程序结构和运行时事实，因此能够跨目标复用。

### 6.2 Frontier 的结构意义

传统覆盖反馈通常把“新覆盖”作为离散事件：输入要么产生新覆盖，要么不产生新覆盖。Frontier 为这种二值信号增加了结构上下文：

1. 它说明当前输入已经抵达未覆盖区域的直接前驱；
2. 它区分不同未覆盖区域的可达性和深度；
3. 它允许在尚未产生新覆盖时，根据输入距离潜在目标的远近进行排序。

因此，frontier 是从覆盖集合到定向结构反馈的第一个桥梁。

## 7. 有向结构距离

### 7.1 目标距离

对于 frontier 目标集合 F_D = {v | (u,v) ∈ Frontier(O)}，StateForge 在 ICFG 上计算从任意节点到目标的有向最短路径：

$$
d(v, F_D) = \min_{t\in F_D} dist_G(v,t),
$$

其中 dist_G(v,t) 是沿 ICFG 有向边从 v 到 t 的最短路径长度。实现上从目标节点反向遍历 ICFG，一次反向 BFS 即可得到所有节点到目标集合的距离。

若节点无法到达任一目标，则将其视为不可达，不参与有效距离排序。对于当前实现中没有显式目标的场景，所有 frontier 的未覆盖终点默认构成目标集合。

### 7.2 输入结构距离

对于输入 x，其基本块集合记为 B(x)。输入到当前 frontier 集合的距离定义为：

$$
d(x, F_D) = \min_{v\in B(x)} d(v,F_D).
$$

该定义并不要求输入已经覆盖 frontier 目标，只要求其当前执行区域在静态 ICFG 上接近某个目标。因而，两个均未产生新覆盖的输入也可以被区分：一个输入可能停留在目标前八条边的位置，另一个输入可能已经到达目标前一条边的位置。

## 8. 结构化 Seed 评分与 Corpus 反馈

第一版 StateForge 使用简单、可解释的结构评分：

$$
Score(x) =
\begin{cases}
\frac{1}{1+d(x,F_D)}, & B(x)\neq\emptyset \land d(x,F_D)\text{ 可达},\\
0, & \text{其他情况}.
\end{cases}
$$

该函数满足两个直观性质：距离越小，分数越高；没有有效执行轨迹或无法到达当前目标的输入不会被错误地赋予高分。实际 corpus 排序采用分层键：首先比较 Score，再比较输入覆盖的基本块数量和边数量，以便在结构距离相同的情况下保留覆盖更广的输入。

对一轮输入执行结果，StateForge 生成如下 seed 级元数据：

~~~
seed identifier
structural score
covered basic blocks
covered ICFG edges
nearest target/frontier distance
execution exception（若有）
~~~

这些元数据构成 corpus prioritization adapter 的输入。Atheris 仍负责生成和执行下一批变异输入；StateForge 只为 corpus 管理或实验驱动器提供优先级信息，不直接实现字节级 mutation、havoc、crossover 或独立的 fuzz loop。这样既保持了 Atheris 的成熟执行能力，也使结构反馈算法能够在不重写底层引擎的情况下进行替换和消融。

## 9. 完整反馈算法

下面给出一轮结构反馈的抽象流程。StaticAnalyze 只执行一次，后续 fuzzing 轮次复用同一份 ICFG。

~~~
算法 1：静态—动态结构反馈
输入：目标程序 P，初始 corpus C，fuzzing 预算 B
输出：覆盖结果、frontier 集合以及 seed 排序

1:  G ← StaticAnalyze(P)                  // 构建 ProgramIR/ICFG
2:  while budget(B) 未耗尽 do
3:      X ← Atheris.ExecuteAndMutate(C)    // Atheris 负责变异与执行
4:      T ← TraceCollect(X)                // 收集带请求边界的有序事件
5:      M ← MapToBasicBlocks(T, G)          // 运行时轨迹映射
6:      O ← BuildOverlay(G, M)              // 节点/边覆盖和命中计数
7:      F ← {(u,v) | (u,v)∈E, u∈C_V, v∉C_V}
8:      D ← ReverseBFS(G, destinations(F))  // 计算有向结构距离
9:      for each input x ∈ X do
10:         s[x] ← 1 / (1 + min distance of blocks in M(x))
11:     end for
12:     C ← RankCorpus(X, s, coverage statistics)
13: end while
14: return O, F, C
~~~

第 3 行明确表示 StateForge 与 Atheris 的职责边界；第 5 至 12 行构成本文的核心反馈路径。随着新输入被执行，C_V 和 C_E 增长，frontier 会发生迁移，距离目标集合也会被重新计算，因此反馈信号随探索进展动态更新。

## 10. 正确性约束

为避免结构反馈因为实现细节而产生伪路径，StateForge 遵循以下不变量。

**不变量一：请求隔离。** 不同输入的运行时事件不能产生动态相邻边。动态边只在相同 request_id 内建立。

**不变量二：块范围一致性。** 一个运行时行号只能映射到包含该行号且属于相同文件和函数的基本块；分支体语句不计入只保存条件的 branch block。

**不变量三：调用点锚定。** CALL 边必须从包含真实 AST Call 节点的调用者基本块出发。无法解析的调用不得被挂接到函数首块或其他近似位置。

**不变量四：边类型保真。** 动态覆盖必须与 (src, dst, kind) 三元组匹配。仅有相同端点但边类型不同的静态边不能被重复计数。

**不变量五：距离有向性。** 距离只能沿 ICFG 中定义的有向边计算，不允许通过反向边、状态关系或数据依赖隐式穿越程序控制流。

这组约束保证了 overlay、frontier 和距离均建立在同一套可解释的程序结构之上，也为后续实验中的日志审计和结果复现提供基础。

## 11. 复杂度分析

设静态 ICFG 包含 |V| 个基本块和 |E| 条边，一轮运行时轨迹包含 |T| 个事件，输入数量为 |X|。

静态 CFG/ICFG 构建的复杂度取决于 AST 大小和调用解析过程；在已有 AST 和索引的情况下，边的插入与图遍历为 O(|V|+|E|)。运行时映射在按文件、函数和行号建立索引后，对单个事件的匹配为局部查找，整轮复杂度约为 O(|T|)。frontier 扫描为 O(|E|)。对一个目标集合执行反向 BFS 为 O(|V|+|E|)，因此一轮反馈的主要开销为：

$$
O(|T| + |V| + |E|).
$$

该开销与目标执行和 Atheris 变异过程解耦，适合在 fuzzing 轮次之间批量计算。后续若需要同时维护多个目标，可复用反向距离图或增量更新覆盖集合，进一步降低重复计算成本。

## 12. 设计边界与可扩展性

本文第一阶段刻意将反馈限定为控制流结构反馈。以下能力不属于当前创新点的必要组成部分：状态变量推断、LLM 状态提升、HTTP 参数语义推理、字典生成、快照恢复以及自定义 mutation。这样做有两个原因。首先，结构反馈可以在不依赖具体 Web 框架和业务字段的情况下进行验证；其次，各个扩展都可以在相同的 ICFG 和 overlay 之上以独立反馈维度加入，从而开展清晰的消融实验。

当前静态调用解析对动态属性分派、库函数和跨模块同名函数仍存在不确定性。StateForge 不将这些不确定调用静默地当作准确边，而是记录 unresolved callsite，并在覆盖和实验报告中保留这一信息。该选择牺牲了一部分静态图完整性，但避免了由伪造 CALL/RETURN 边导致的虚假距离和错误 frontier。

在后续版本中，可以将评分函数扩展为：

$$
Score(x) = \alpha D(x) + \beta R(x) + \gamma H(x) + \delta C(x),
$$

其中 D 表示结构距离，R 表示可达性，H 表示命中稀有度，C 表示调用上下文价值。当前工作只验证最小的距离评分，以确保每个反馈因素都可以通过独立消融实验评估。

## 13. 创新点总结

本文的第一个创新点是提出并实现了一种面向 Python 程序的静态—动态结构反馈机制。该机制将基本块级 ICFG 与有序运行时执行轨迹统一到同一个程序表示中，进而完成以下闭环：

1. 从 AST 构建带有 CALL/RETURN 语义的基本块级 ICFG；
2. 将运行时文件、函数和行号映射为有序基本块轨迹；
3. 在静态图上叠加动态覆盖，识别 covered-to-uncovered structural frontier；
4. 使用有向跨过程最短距离评价输入与未探索区域的结构接近程度；
5. 以结构分数对 corpus 进行排序，并通过轻量 adapter 将结果反馈给 Atheris。

该设计把 Atheris 的成熟变异/执行能力与 StateForge 的程序结构分析能力分离开来。Atheris 回答“如何生成并执行下一个输入”，StateForge 回答“当前输入在程序结构中的位置、哪些可达区域尚未探索以及哪些输入更值得继续保留”。由此，StateForge 的研究贡献被明确限定为一种可解释、可复现、可独立消融的结构反馈层，而不是另一个重新实现 mutation 和执行循环的 Fuzzer。
