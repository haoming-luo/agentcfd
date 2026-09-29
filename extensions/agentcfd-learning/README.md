# AgentCFD Learning

The first official learning extension for AgentCFD. It converts a verified
scalar campaign dataset into deterministic, framework-neutral train and
validation batches. Normalization is fitted only on training samples, units and
case identity remain visible, and importing the package does not import a
neural-network framework.

```python
from agentcfd_learning import prepare_dataset

bundle = prepare_dataset("datasets/pressure-map", seed=17)
x_train, y_train = bundle.train.to_numpy()
```

`agentfem_sample_records(path)` returns plain records whose keys match
`agentfem.datasets.Sample`, without importing AgentFEM or creating a runtime
dependency between the products.

The source dataset remains the system of record. The bundle carries its
content digest and the complete AgentCFD training plan rather than inventing a
second opaque dataset format.
