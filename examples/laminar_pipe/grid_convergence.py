from agentcfd.verification import GridSolution, grid_convergence_index


# Replace these demonstrative values with one consistently recovered quantity
# from three geometrically similar CFD grids.
study = grid_convergence_index(
    (
        GridSolution(0.40, 1.080, "coarse"),
        GridSolution(0.20, 1.020, "medium"),
        GridSolution(0.10, 1.005, "fine"),
    )
)

print(study.to_dict())
