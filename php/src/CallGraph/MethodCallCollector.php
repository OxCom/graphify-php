<?php

declare(strict_types=1);

namespace CallGraph;

use PhpParser\Node;
use PhpParser\Node\Expr\MethodCall;
use PhpParser\Node\Identifier;
use PHPStan\Analyser\Scope;
use PHPStan\Collectors\Collector;
use PHPStan\Reflection\ClassReflection;

/**
 * Records every `$receiver->method()` whose receiver the type engine could name.
 *
 * A parser can only see types that are written down. The engine also knows the type of a
 * private property assigned in the constructor, of a local, of a return value and of a
 * docblock, which is why this exists at all: on the reference application a parser-only
 * pass left three quarters of callables with no outgoing call edge.
 *
 * Nothing here is reported unless the engine produced a class reflection for the receiver.
 * A name-only fallback — matching `->get()` to any class owning a `get` — manufactures edges
 * that look identical to real ones, and a wrong edge is worse than a missing one.
 *
 * @implements Collector<MethodCall, list<array<string, mixed>>>
 */
final class MethodCallCollector implements Collector
{
    public function getNodeType(): string
    {
        return MethodCall::class;
    }

    /**
     * @param MethodCall $node
     * @return list<array<string, mixed>>|null
     */
    public function processNode(Node $node, Scope $scope): ?array
    {
        if (!$node->name instanceof Identifier) {
            return null;
        }

        $callerClass = $scope->getClassReflection();

        if ($callerClass === null) {
            return null;
        }

        $callerMethod = $scope->getFunctionName();

        if ($callerMethod === null) {
            return null;
        }

        $methodName = $node->name->toString();
        $receiverType = $scope->getType($node->var);
        $edges = [];

        foreach ($receiverType->getObjectClassReflections() as $receiver) {
            $edge = $this->edgeTo($receiver, $methodName, $callerClass->getName(), $callerMethod, $node, $scope);

            if ($edge === null) {
                continue;
            }

            $edges[] = $edge->toArray();
        }

        if ($edges === []) {
            return null;
        }

        return $edges;
    }

    /**
     * @return CallEdge|null
     */
    private function edgeTo(
        ClassReflection $receiver,
        string $methodName,
        string $callerClass,
        string $callerMethod,
        MethodCall $node,
        Scope $scope,
    ): ?CallEdge {
        if (!$receiver->hasMethod($methodName)) {
            return null;
        }

        // The declaring class, not the receiver: an inherited method lives in the parent, and an
        // edge to the child would point at a body that does not exist there.
        $declaring = $receiver->getMethod($methodName, $scope)->getDeclaringClass();

        return CallEdge::create(
            $callerClass,
            $callerMethod,
            $declaring->getName(),
            $methodName,
            $declaring->isInterface(),
            $node->getStartLine(),
        );
    }
}
