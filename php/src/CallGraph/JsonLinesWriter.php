<?php

declare(strict_types=1);

namespace CallGraph;

use RuntimeException;

/**
 * Writes one JSON object per line.
 *
 * Line-delimited rather than one JSON document: a truncated run — the analyser killed on a
 * memory limit, a timeout — still leaves every completed record readable, and the consumer
 * drops the one broken tail line instead of losing the whole file.
 */
final class JsonLinesWriter implements CallEdgeWriter
{
    /**
     * Set by whoever launches the analyser, so one checkout can be analysed into different
     * destinations without editing the committed phpstan.neon.
     */
    public const PATH_ENV = 'PHP_CALL_GRAPH_OUT';

    private const DEFAULT_PATH = 'php-call-graph.jsonl';

    private readonly string $path;

    public function __construct(?string $path = null)
    {
        $this->path = $path ?? self::pathFromEnvironment();
    }

    public function write(iterable $edges): void
    {
        $handle = @fopen($this->path, 'wb');

        if ($handle === false) {
            throw new RuntimeException(sprintf('Cannot open %s for writing', $this->path));
        }

        foreach ($edges as $edge) {
            $line = json_encode($edge->toArray(), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

            if ($line === false) {
                continue;
            }

            fwrite($handle, $line . "\n");
        }

        fclose($handle);
    }

    private static function pathFromEnvironment(): string
    {
        $configured = getenv(self::PATH_ENV);

        if (!is_string($configured) || $configured === '') {
            return self::DEFAULT_PATH;
        }

        return $configured;
    }
}
