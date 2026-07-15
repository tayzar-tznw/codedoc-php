<?php
declare(strict_types=1);

namespace Web\Support;

final class LocalReporter
{
    public function __construct(private Logger $logger)
    {
    }

    public function report(): void
    {
        // $this->logger is the LOCAL Web\Support\Logger, not Shared\Logging\Logger.
        $this->logger->debug('local report');
    }
}
