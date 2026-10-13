<?php

declare(strict_types=1);

namespace CallGraph;

use PhpParser\Node;
use PhpParser\Node\Expr\MethodCall;
use PhpParser\Node\Identifier;
use PHPStan\Analyser\Scope;
use PHPStan\Collectors\Collector;
use PHPStan\Reflection\ClassReflection;
use PHPStan\Reflection\Php\PhpMethodReflection;
use PHPStan\Type\IntersectionType;
use PHPStan\Type\Type;
use PHPStan\Type\UnionType;

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

        // A union `A|B` is one of two objects, so every arm is a body that can run and each arm
        // is decided on its own: `(A&B)|C` keeps C and drops the intersection, `(A&B)|null` has
        // nothing left. Decided on the receiver as a whole, the intersection inside a union was
        // never seen and A, B and C all went out under one call position.
        $arms = $receiverType instanceof UnionType ? $receiverType->getTypes() : [$receiverType];

        foreach ($arms as $arm) {
            foreach ($this->armEdges($arm, $methodName, $callerClass->getName(), $callerMethod, $node, $scope) as $record) {
                $edges[] = $record;
            }
        }

        if ($edges === []) {
            return null;
        }

        return $edges;
    }

    /**
     * The records for one arm of the receiver type, or none when the arm is ambiguous.
     *
     * An intersection `A&B` is one object; when two constituents declare the method the type
     * does not say which body runs, and emitting both would attribute a call to a body that
     * never executes. Refused, as the parser layer refuses the same shape. Keyed on the
     * declaring class so `A&B` with B extending A still resolves to the one body in A.
     *
     * @return list<array<string, mixed>>
     */
    private function armEdges(
        Type $arm,
        string $methodName,
        string $callerClass,
        string $callerMethod,
        MethodCall $node,
        Scope $scope,
    ): array {
        $records = [];
        $declaringClasses = [];

        foreach ($arm->getObjectClassReflections() as $receiver) {
            $edge = $this->edgeTo($receiver, $methodName, $callerClass, $callerMethod, $node, $scope);

            if ($edge === null) {
                continue;
            }

            $record = $edge->toArray();
            $records[] = $record;
            $declaringClasses[$record['calleeClass']] = true;
        }

        if ($arm instanceof IntersectionType && count($declaringClasses) > 1) {
            return [];
        }

        return $records;
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
        // edge to the child would point at a body that does not exist there. For a trait method
        // the declaring class is the class using the trait, whose file holds no such body either,
        // so the trait is named instead. Only the native reflection knows its trait; a wrapped
        // reflection falls back to the class, as before.
        $method = $receiver->getMethod($methodName, $scope);
        $declaring = $method instanceof PhpMethodReflection
            ? ($method->getDeclaringTrait() ?? $method->getDeclaringClass())
            : $method->getDeclaringClass();

        return CallEdge::create(
            $callerClass,
            $callerMethod,
            $declaring->getName(),
            $methodName,
            $declaring->isInterface(),
            $node->getStartLine(),
            '',
            // The method name's token, not the expression's: in a chain `$a->get()->get()` every
            // link starts at `$a`, and the name is the one token each call owns alone.
            $node->name->getStartTokenPos(),
            $declaring->getFileName() ?: '',
        );
    }
}
