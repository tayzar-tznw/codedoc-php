<?php
declare(strict_types=1);

namespace App\S02_TypedReceivers;

class Invoice
{
    public function send(): string
    {
        return 'invoice-sent';
    }
}
