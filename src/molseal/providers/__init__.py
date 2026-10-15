"""Key providers backed by key management services. Each module imports its vendor SDK only on the
default-client path, so importing a module or constructing a provider with an injected client needs
no SDK; `pip install molseal[aws|gcp|azure]` installs one. The Infisical provider needs nothing."""
