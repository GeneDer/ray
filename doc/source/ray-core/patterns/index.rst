.. _core-patterns:

Design Patterns & Anti-patterns
===============================

This section is a collection of common design patterns and anti-patterns for writing Ray applications.

.. toctree::
    :maxdepth: 1

    nested-tasks
    generators
    limit-pending-tasks
    limit-running-tasks
    concurrent-operations-async-actor
    actor-sync
    tree-of-actors
    pipelining
    return-ray-put
    nested-ray-get
    ray-get-loop
    unnecessary-ray-get
    ray-get-submission-order
    ray-get-too-many-objects
    too-fine-grained-tasks
    redefine-task-actor-loop
    pass-large-arg-by-value
    closure-capture-large-objects
    global-variables
    out-of-band-object-ref-serialization
    fork-new-processes
