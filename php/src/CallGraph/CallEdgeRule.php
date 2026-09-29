<?php

declare(strict_types=1);

namespace CallGraph;

use PhpParser\Node;
use PHPStan\Analyser\Scope;
use PHPStan\Node\CollectedDataNode;
use PHPStan\Rules\Rule;

/**
 * Turns everything the collector gathered into the output file.
 *
 * A rule rather than a second collector because `CollectedDataNode` is delivered once, in the
 * main process, after every worker has finished — the only point at which the whole set of
 * edges exists in one place.
 *
 * It reports no analysis errors. The output is the file; PHPStan's exit code stays whatever the
 * project's own rules make it, and the consumer is expected to read the file regardless.
 *
 * @implements Rule<CollectedDataNode>
 */
final class CallEdgeRule implements Rule
{
    public function __construct(private readonly CallEdgeWriter $writer)
    {
    }

    public function getNodeType(): string
    {
        return CollectedDataNode::class;
    }

    /**
     * @param CollectedDataNode $node
     * @return list<never>
     */
    public function processNode(Node $node, Scope $scope): array
    {
        $this->writer->write($this->edges($node));

        return [];
    }

    /**
     * @return iterable<CallEdge>
     */
    private function edges(CollectedDataNode $node): iterable
    {
        foreach ($node->get(MethodCallCollector::class) as $file => $perNode) {
            foreach ($perNode as $records) {
                foreach ($records as $record) {
                    // The file comes from the key rather than from the record: the analyser owns
                    // path normalisation, and a second copy would drift from it.
                    yield CallEdge::fromArray($record, (string) $file);
                }
            }
        }
    }
}
