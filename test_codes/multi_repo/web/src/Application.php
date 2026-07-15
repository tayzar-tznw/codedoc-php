<?php
declare(strict_types=1);

namespace Web;

use Cake\Core\ContainerInterface;
use Web\Order\OrderService;
use Web\Time\SystemClock;

final class Application
{
    public function services(ContainerInterface $container): void
    {
        // OrderService (web) is injected with the shared Logger — cross-repo inject.
        $container->add(OrderService::class)
            ->addArgument(\Shared\Logging\Logger::class);

        // The shared Clock interface is implemented by web's SystemClock — cross-repo bind.
        $container->add(\Shared\Contracts\Clock::class, SystemClock::class);
    }
}
