<?php
declare(strict_types=1);

namespace App\Model\Entity;

use Cake\ORM\Entity;

class Article extends Entity
{
    protected function _getAuthorName(): string
    {
        return trim(($this->author_first ?? '') . ' ' . ($this->author_last ?? ''));
    }
}
