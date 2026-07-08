<?php
declare(strict_types=1);

namespace App\S05_InterfaceDispatch;

class StripeGateway implements PaymentGateway
{
    public function authorize(int $amountCents): bool
    {
        return $amountCents > 0;
    }
}
