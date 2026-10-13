<?php

namespace Eval\Handler;

interface Logger
{
    public function debug(string $message, array $context = []): void;
}

final class Message
{
    public function __construct(private readonly int $id)
    {
    }

    public function getId(): int
    {
        return $this->id;
    }
}

final class User
{
    public function getObjectId(): string
    {
        return 'object-id';
    }

    public function getAvatar(): ?string
    {
        return null;
    }
}

abstract class EntityRepository
{
    public function find(int $id): ?User
    {
        return null;
    }
}

final class UserRepository extends EntityRepository
{
    public function disableListeners(): void
    {
    }
}

final class DirectoryUsers
{
    public function findById(string $objectId): ?User
    {
        return null;
    }
}

final class DirectoryAvatars
{
    public function update(User $user, string $avatar): void
    {
    }
}

final class DirectoryClient
{
    public function __construct(
        private readonly DirectoryUsers $users,
        private readonly DirectoryAvatars $avatar,
    ) {
    }

    public function users(): DirectoryUsers
    {
        return $this->users;
    }

    public function avatar(): DirectoryAvatars
    {
        return $this->avatar;
    }
}

final class AvatarHandler
{
    public function __construct(
        private readonly Logger $logger,
        private readonly UserRepository $users,
        private readonly DirectoryClient $directory,
    ) {
    }

    public function __invoke(Message $message): void
    {
        $this->process($message);
    }

    public function process(Message $message): void
    {
        $this->users->disableListeners();

        $user = $this->users->find($message->getId());

        if (empty($user)) {
            return;
        }

        $directoryUser = $this->directory->users()->findById($user->getObjectId());

        if (empty($directoryUser)) {
            return;
        }

        $avatar = $user->getAvatar();

        if (empty($avatar)) {
            return;
        }

        $this->logger->debug('Updating a directory avatar', ['user' => $user->getObjectId()]);

        $this->directory->avatar()->update($directoryUser, $avatar);
    }
}
