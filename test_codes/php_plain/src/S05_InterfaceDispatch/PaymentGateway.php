<?php
declare(strict_types=1);

namespace App\S05_InterfaceDispatch;

interface PaymentGateway
{
    public function authorize(int $amountCents): bool;
}
