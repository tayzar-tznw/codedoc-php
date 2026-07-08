<?php
declare(strict_types=1);

namespace Billing\Model\Entity;

use Cake\ORM\Entity;

class Invoice extends Entity
{
    protected function _getTotalLabel(): string
    {
        return '$' . number_format((float)($this->total ?? 0), 2);
    }
}
