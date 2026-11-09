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
        private readonly int $callPos,
        private readonly string $calleeFile,
        private readonly string $callerFile = '',
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
        int $callPos = -1,
        string $calleeFile = '',
    ): self {
        $kind = match ($calleeIsInterface) {
            true => self::RECEIVER_INTERFACE,
            false => self::RECEIVER_CLASS,
        };

        return new self($callerClass, $callerMethod, $calleeClass, $calleeMethod, $kind, $line, $file, $callPos, $calleeFile);
    }

    /**
     * `$contextFile` is the file the analyser was in. It is the body's file too, except for a
     * trait body: the collector then names the trait's file, and the class's file is kept apart.
     *
     * @param array<string, mixed> $raw
     */
    public static function fromArray(array $raw, string $contextFile): self
    {
        $file = (string) ($raw['file'] ?? '');

        return new self(
            (string) ($raw['callerClass'] ?? ''),
            (string) ($raw['callerMethod'] ?? ''),
            (string) ($raw['calleeClass'] ?? ''),
            (string) ($raw['calleeMethod'] ?? ''),
            (string) ($raw['calleeKind'] ?? self::RECEIVER_CLASS),
            (int) ($raw['line'] ?? 0),
            $file !== '' ? $file : $contextFile,
            (int) ($raw['callPos'] ?? -1),
            (string) ($raw['calleeFile'] ?? ''),
            $contextFile,
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
            // Token offset of the called method's name. Two arms of one union receiver share it;
            // two calls of the same method on one line do not, and the Python half tells them apart.
            'callPos' => $this->callPos,
            // Where the declaring class lives, '' for a built-in. The consumer tells a vendor
            // class from a project class by this path, never by guessing from the namespace.
            'calleeFile' => $this->calleeFile,
            // The file of the class the body was analysed in. Equal to `file` except for a trait
            // body, which is analysed once per using class; a consumer must not place it there.
            'callerFile' => $this->callerFile,
        ];
    }
}
