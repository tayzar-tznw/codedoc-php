<?php
declare(strict_types=1);

namespace App\Service;

class InvoiceMailer
{
    public function deliver(int $invoiceId): string
    {
        return 'delivered:' . $invoiceId;
    }
}
