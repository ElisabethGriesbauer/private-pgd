from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy import sparse
from scipy.special import softmax

from inference.pgm.graphical_model import GraphicalModel
from inference.pgm.inference import FactoredInference
from inference.privpgd.particle_model import ParticleModel
from mechanisms.mechanism import Mechanism

if TYPE_CHECKING:
    from inference.dataset import Dataset
    from inference.privpgd.inference import AdvancedSlicedInference


class MWEM(Mechanism):
    def __init__(
        self,
        hp: Dict[str, Any],
        bounded: bool = True,
        prng: np.random = np.random,
    ):
        """
        Initializes the MWEM mechanism for differential privacy.

        Args:
            prng (np.random): Pseudo Random Number Generator. Defaults to None.
            hp (Dict[str, Any]): A dictionary of hyperparameters containing epsilon, delta, and degree.
            bounded (bool): Privacy definition (bounded vs unbounded DP).
        """
        super(MWEM, self).__init__(
            epsilon=hp["epsilon"], delta=hp["delta"], bounded=bounded, prng=prng
        )
        self.k = hp["degree"]
        self.rounds = hp["rounds"]
        self.data_init = hp["data_init"]
        self.max_model_size = hp["max_model_size"]
        self.hp = hp

    def worst_approximated(
        self,
        workload_answers: Dict[Tuple[str, ...], np.ndarray],
        est: Union["GraphicalModel", "ParticleModel"],
        workload: List[Tuple[str, ...]],
        eps: float,
        penalty: bool = True,
    ) -> Tuple[str, ...]:
        """
        Selects the worst approximated candidate using the exponential mechanism.

        Args:
            workload_answers (Dict[Tuple[str, ...], np.ndarray]): True answers for the queries.
            est (Union[GraphicalModel, ParticleModel]): The estimated model used for projection.
            workload (List[Tuple[str, ...]]): Workload of queries.
            eps (float): Epsilon value for differential privacy.
            penalty (bool): Flag to apply penalty. Defaults to True.

        Returns:
            Tuple[str, ...]: The selected worst approximated candidate.
        """
        errors = np.array([])
        for cl in workload:
            bias = est.domain.size(cl) if penalty else 0
            x = workload_answers[cl]
            xest = est.project(cl).datavector()
            errors = np.append(errors, np.abs(x - xest).sum() - bias)

        prob = softmax(0.5 * eps / self.sensitivity * (errors - errors.max()))
        key = np.random.choice(len(errors), p=prob)
        return workload[key]

    def run(
        self,
        data: "Dataset",
        workload: List[Tuple[str, ...]],
        engine: Union["FactoredInference", "AdvancedSlicedInference"],
        alpha: float = 0.9,
        records: Optional[int] = None,
    ) -> Tuple["Dataset", float]:
        """
        Runs the MWEM mechanism to generate a synthetic dataset.

        Args:
            data (Dataset): The original dataset.
            workload (Optional[List[Tuple[str, ...]]]): A list of queries as tuples of attributes. Defaults to None.
            engine (Union[FactoredInference, AdvancedSlicedInference]): The inference engine used for estimation.
            alpha (float): Alpha parameter for controlling noise. Defaults to 0.9.
            records(Optional[int]): Number of samples of the generated dataset. Defaults to None, same as original dataset.

        Returns:
            Tuple[Dataset, float]: The synthetic dataset and the associated loss.
        """
        rounds = min(len(workload), self.rounds)
        rho = self.rho
        rho_per_round = rho / rounds
        sigma = np.sqrt(0.5 / (alpha * rho_per_round))
        exp_eps = np.sqrt(8 * (1 - alpha) * rho_per_round)
        domain = data.domain
        total = data.records if self.bounded else None

        def size(cliques):
            return GraphicalModel(domain, cliques).size * 8 / 2**20

        workload_answers = {
            cl: data.project(cl).datavector() for cl in workload
        }

        measurements = []
        if isinstance(engine, FactoredInference):
            est, _ = engine.estimate(measurements, total)
        else:
            est = ParticleModel(
                data.domain,
                embedding=engine.embedding,
                n_particles=engine.n_particles,
                data_init=self.data_init,
            )
        cliques = []
        for i in range(1, rounds + 1):
            if isinstance(engine, FactoredInference):
                candidates = [
                    cl
                    for cl in workload
                    if size(cliques + [cl]) <= self.max_model_size * i / rounds
                ]
                ax = self.worst_approximated(
                    workload_answers, est, candidates, exp_eps
                )
                print(
                    "Round",
                    i,
                    "Selected",
                    ax,
                    "Model Size (MB)",
                    est.size * 8 / 2**20,
                )
            else:
                ax = self.worst_approximated(
                    workload_answers, est, workload, exp_eps
                )

            n = domain.size(ax)
            x = data.project(ax).datavector()

            y = x + np.random.normal(
                loc=0, scale=self.marginal_sensitivity * sigma, size=n
            )
            Q = sparse.eye(n)
            measurements.append((Q, y, 1.0, ax))
            est, loss = engine.estimate(measurements, total)
            cliques.append(ax)

        print("Generating Data...")
        return est.synthetic_data(records), loss
