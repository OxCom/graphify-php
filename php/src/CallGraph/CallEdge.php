<?php

declare(strict_types=1);

namespace CallGraph;

/**
 * One resolved `$receiver->method()`: who called, what was called, where.
 *
 * This class is the only place in the PHP extension that knows the wire shape. The collector
 * runs in analyser workers and the rule runs in the main process, so records cross a
 * serialisation boundary as plain arrays; keeping the key names here means a shape change is
 * one edit rather than a hunt through two processes.
 */
final class CallEdge
{
    public const RECEIVER_CLASS = 'class';
    public const RECEIVER_INTERFACE = 'interface';

    private function __construct(
        private readonly string $callerClass,
        private readonly string $callerMethod,
        private readonly string $calleeClass,
        private readonly string $calleeMethod,
        private readonly string $calleeKind,
        private readonly int $line,
        private readonly string $file,
    ) {
    }

    public static function create(
        string $callerClass,
        string $callerMethod,
        string $calleeClass,
        string $calleeMethod,
        bool $calleeIsInterface,
        int $line,
        string $file = '',
    ): self {
        $kind = match ($calleeIsInterface) {
            true => self::RECEIVER_INTERFACE,
            false => self::RECEIVER_CLASS,
        };

        return new self($callerClass, $callerMethod, $calleeClass, $calleeMethod, $kind, $line, $file);
    }

    /**
     * @param array<string, mixed> $raw
     */
    public static function fromArray(array $raw, string $file): self
    {
        return new self(
            (string) ($raw['callerClass'] ?? ''),
            (string) ($raw['callerMethod'] ?? ''),
            (string) ($raw['calleeClass'] ?? ''),
            (string) ($raw['calleeMethod'] ?? ''),
            (string) ($raw['calleeKind'] ?? self::RECEIVER_CLASS),
            (int) ($raw['line'] ?? 0),
            $file,
        );
    }

    /**
     * @return array<string, mixed>
     */
    public function toArray(): array
    {
        return [
            'callerClass' => $this->callerClass,
            'callerMethod' => $this->callerMethod,
            'calleeClass' => $this->calleeClass,
            'calleeMethod' => $this->calleeMethod,
            'calleeKind' => $this->calleeKind,
            'line' => $this->line,
            'file' => $this->file,
        ];
    }
}
