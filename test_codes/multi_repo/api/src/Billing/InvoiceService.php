<?php
declare(strict_types=1);

namespace Api\Billing;

use Shared\Money\Price;
use Shared\Logging\Logger;

final class InvoiceService
{
    public function __construct(private Logger $logger)
    {
    }

    public function charge(Price $amount): string
    {
        $this->logger->info('charging invoice');

        return $amount->format();
    }
}
