<?php

declare(strict_types=1);

namespace CallGraph;

/**
 * Where resolved edges go once the analysis is over.
 *
 * Separate from the rule so that changing the destination — a different file format, a socket,
 * a test double — never touches the code that decides which calls are real.
 */
interface CallEdgeWriter
{
    /**
     * @param iterable<CallEdge> $edges
     */
    public function write(iterable $edges): void;
}
