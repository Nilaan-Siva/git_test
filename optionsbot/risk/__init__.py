"""The risk manager: the single veto gate every OrderIntent must pass before it becomes an
Order. See risk/manager.py's module docstring for the core design invariant: this package can
only ever reject or size down what a strategy proposes -- it never loosens a limit and never
invents a trade on its own."""
