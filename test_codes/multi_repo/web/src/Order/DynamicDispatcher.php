<?php
declare(strict_types=1);

namespace Web\Order;

final class DynamicDispatcher
{
    public function run(string $name, array $handlers): void
    {
        // Runtime-computed receiver — its type cannot be determined statically,
        // so no cross-repo edge is produced even if a matching method exists
        // in another repo (documented partial-coverage boundary).
        $handler = $handlers[$name];
        $handler->handle();
    }
}
